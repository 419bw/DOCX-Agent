"""
MessageManager 单元测试 — 防御 DeepSeek API 400 错误的修复验证

背景:
  DeepSeek 严格校验 messages 数组 schema, 比 OpenAI 严格
  - {"role": "assistant", "tool_calls": []} → 被视为"想调工具但 ID 为空", 后续 tool 消息变孤儿 → 400
  - orphan tool 消息 (无匹配 assistant tool_call) → 400
  - 空 tool_call_id → 400

本测试覆盖:
  1) build_request_messages 脱空 tool_calls 字段
  2) build_request_messages 丢孤儿 tool 消息 (post-validation pass)
  3) _sanitize_entries 一次性清理旧 session 数据
  4) 现有 dedup 逻辑回归测试

运行: python -m pytest src/test_context_manager.py -v
或:   python src/test_context_manager.py  (内联 __main__)
"""

import sys
from pathlib import Path

# 把 src/ 加到 sys.path, 让 `from context_manager import MessageManager` 可用
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from context_manager import MessageManager


# ─── 工具函数 ────────────────────────────────────────


def _find_by_role(messages: list[dict], role: str) -> list[dict]:
    return [m for m in messages if m.get("role") == role]


def _build_tool_call(call_id: str, name: str = "ls", args: str = "{}") -> dict:
    """构造一个合法的 tool_call dict"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


# ─── 1) 脱空 tool_calls 字段 ──────────────────────────


def test_strip_empty_tool_calls_in_assistant():
    """
    旧 session 数据: {"role": "assistant", "tool_calls": []} 必须被脱掉
    不脱会触发 DeepSeek 400 (Messages with role 'tool' must be ...)
    """
    mgr = MessageManager("sys")
    mgr.append_user("hi")
    # 模拟 c2d4322 之前的旧数据 (append_assistant 写过空 tool_calls 数组)
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [],   # ← 旧版本写入的空数组
        "content": "some text",
    })
    mgr.append_user("next")

    msgs = mgr.build_request_messages("state")

    assistants = _find_by_role(msgs, "assistant")
    assert len(assistants) == 1
    a = assistants[0]
    assert "tool_calls" not in a, (
        f"empty tool_calls=[] 字段必须被脱掉, 实际: {a}"
    )
    assert a["content"] == "some text"
    print("  ✓ test_strip_empty_tool_calls_in_assistant")


# ─── 2) 丢孤儿 tool 消息 (post-validation) ─────────────


def test_drop_orphan_tool_message():
    """
    tool 消息无对应 assistant(tool_calls) → 必须在 post-validation pass 被丢
    场景: 旧 session 残缺 / 外部篡改
    """
    mgr = MessageManager("sys")
    mgr.append_user("hi")
    # 模拟"assistant 丢了但 tool 消息残留"
    mgr._entries.append({"role": "tool", "tool_call_id": "orphan_X", "content": "stale"})
    mgr.append_user("next")

    msgs = mgr.build_request_messages("state")

    tool_msgs = _find_by_role(msgs, "tool")
    assert tool_msgs == [], (
        f"孤儿 tool 消息必须被丢, 实际残留: {tool_msgs}"
    )
    # user 消息必须保留
    users = _find_by_role(msgs, "user")
    assert len(users) == 2
    print("  ✓ test_drop_orphan_tool_message")


def test_drop_tool_message_with_empty_id():
    """
    tool_call_id 为空字符串的 tool 消息也必须被丢
    (空 id 永远不可能有匹配的 assistant tool_call)
    """
    mgr = MessageManager("sys")
    mgr.append_user("hi")
    mgr._entries.append({"role": "tool", "tool_call_id": "", "content": "stale"})
    mgr.append_user("next")

    msgs = mgr.build_request_messages("state")

    tool_msgs = _find_by_role(msgs, "tool")
    assert tool_msgs == [], (
        f"空 tool_call_id 必须被丢, 实际残留: {tool_msgs}"
    )
    print("  ✓ test_drop_tool_message_with_empty_id")


# ─── 3) 保留正常配对 ──────────────────────────────────


def test_preserve_valid_assistant_tool_pair():
    """
    正常的 assistant(tool_calls) + tool 配对必须保留
    """
    mgr = MessageManager("sys")
    mgr.append_user("hi")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [_build_tool_call("X", "ls", "{}")],
        "content": "",
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "X", "content": "ok"})
    mgr.append_user("next")

    msgs = mgr.build_request_messages("state")

    assistants = _find_by_role(msgs, "assistant")
    assert len(assistants) == 1
    assert "tool_calls" in assistants[0]
    assert assistants[0]["tool_calls"][0]["id"] == "X"

    tool_msgs = _find_by_role(msgs, "tool")
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "X"
    print("  ✓ test_preserve_valid_assistant_tool_pair")


def test_preserve_multiple_tool_calls_in_one_assistant():
    """
    一个 assistant 消息里多个 tool_calls, 每个都有对应 tool 结果 → 全部保留
    """
    mgr = MessageManager("sys")
    mgr.append_user("hi")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [
            _build_tool_call("A", "ls", "{}"),
            _build_tool_call("B", "read", '{"path": "/tmp/x"}'),
        ],
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "A", "content": "out_a"})
    mgr._entries.append({"role": "tool", "tool_call_id": "B", "content": "out_b"})
    mgr.append_user("next")

    msgs = mgr.build_request_messages("state")
    tool_msgs = _find_by_role(msgs, "tool")
    assert len(tool_msgs) == 2
    assert {t["tool_call_id"] for t in tool_msgs} == {"A", "B"}
    print("  ✓ test_preserve_multiple_tool_calls_in_one_assistant")


# ─── 4) 现有 dedup 逻辑回归 ───────────────────────────


def test_dedup_of_write_markdown_draft():
    """
    write_markdown_draft 重复调用同一 target → 旧 tool_call + tool_result 配对删除
    """
    mgr = MessageManager("sys")
    mgr.append_user("v1")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [_build_tool_call(
            "T1", "write_markdown_draft", '{"output_path": "/tmp/a.md"}'
        )],
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "T1", "content": "v1 ok"})

    mgr.append_user("v2 (overwrite)")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [_build_tool_call(
            "T2", "write_markdown_draft", '{"output_path": "/tmp/a.md"}'
        )],
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "T2", "content": "v2 ok"})

    msgs = mgr.build_request_messages("state")

    tool_msgs = _find_by_role(msgs, "tool")
    # 反向扫描: T2 是最新的, 保留; T1 被 dedup 丢
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "T2"
    print("  ✓ test_dedup_of_write_markdown_draft")


def test_dedup_does_not_create_orphan():
    """
    dedup 之后, 被去重掉的 tool result 必须被丢 (避免孤儿)
    """
    mgr = MessageManager("sys")
    mgr.append_user("v1")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [_build_tool_call(
            "T1", "write_markdown_draft", '{"output_path": "/tmp/a.md"}'
        )],
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "T1", "content": "v1"})

    mgr.append_user("v2")
    mgr._entries.append({
        "role": "assistant",
        "tool_calls": [_build_tool_call(
            "T2", "write_markdown_draft", '{"output_path": "/tmp/a.md"}'
        )],
    })
    mgr._entries.append({"role": "tool", "tool_call_id": "T2", "content": "v2"})

    msgs = mgr.build_request_messages("state")

    # 验证: 没有 T1 的孤儿 tool 消息
    for m in msgs:
        if m.get("role") == "tool":
            assert m["tool_call_id"] == "T2", (
                f"dedup 之后被丢弃的 T1 不能以孤儿形式出现: {m}"
            )
    print("  ✓ test_dedup_does_not_create_orphan")


# ─── 5) _sanitize_entries 直接测试 ─────────────────────


def test_sanitize_strips_empty_tool_calls():
    """_sanitize_entries: 脱空 tool_calls 字段"""
    mgr = MessageManager("sys")
    mgr._entries.extend([
        {"role": "user", "content": "u1"},
        {"role": "assistant", "tool_calls": [], "content": "old format"},
        {"role": "user", "content": "u2"},
    ])

    mgr._sanitize_entries()

    a = [e for e in mgr._entries if e.get("role") == "assistant"][0]
    assert "tool_calls" not in a, f"sanitize 后应无 tool_calls 字段, 实际: {a}"
    assert a["content"] == "old format"
    print("  ✓ test_sanitize_strips_empty_tool_calls")


def test_sanitize_drops_orphan_tool():
    """_sanitize_entries: 丢孤儿 tool 消息"""
    mgr = MessageManager("sys")
    mgr._entries.extend([
        {"role": "user", "content": "u1"},
        # orphan: 无对应 assistant(tool_calls) 提供 "orphan_id"
        {"role": "tool", "tool_call_id": "orphan_id", "content": "stale"},
        {"role": "user", "content": "u2"},
    ])

    mgr._sanitize_entries()

    tool_msgs = [e for e in mgr._entries if e.get("role") == "tool"]
    assert tool_msgs == [], f"sanitize 后应无 tool 消息, 实际: {tool_msgs}"
    # user 消息保留
    assert len([e for e in mgr._entries if e.get("role") == "user"]) == 2
    print("  ✓ test_sanitize_drops_orphan_tool")


def test_sanitize_drops_empty_tool_call_id():
    """_sanitize_entries: 丢空 tool_call_id 的 tool 消息"""
    mgr = MessageManager("sys")
    mgr._entries.extend([
        {"role": "user", "content": "u1"},
        {"role": "tool", "tool_call_id": "", "content": "stale"},
    ])

    mgr._sanitize_entries()

    tool_msgs = [e for e in mgr._entries if e.get("role") == "tool"]
    assert tool_msgs == [], f"空 id 的 tool 消息应被丢, 实际: {tool_msgs}"
    print("  ✓ test_sanitize_drops_empty_tool_call_id")


def test_sanitize_preserves_valid_pair():
    """_sanitize_entries: 不影响正常配对"""
    mgr = MessageManager("sys")
    mgr._entries.extend([
        {"role": "user", "content": "u1"},
        {"role": "assistant", "tool_calls": [_build_tool_call("X", "ls", "{}")]},
        {"role": "tool", "tool_call_id": "X", "content": "ok"},
    ])

    mgr._sanitize_entries()

    assert len(mgr._entries) == 3
    a = [e for e in mgr._entries if e.get("role") == "assistant"][0]
    assert a["tool_calls"][0]["id"] == "X"
    print("  ✓ test_sanitize_preserves_valid_pair")


# ─── 6) 集成场景: 旧 session 加载后能正常工作 ──────────


def test_old_session_simulation():
    """
    集成场景: 模拟 c2d4322 之前保存的旧 session
    - 含 tool_calls=[] 的旧消息
    - 含一个孤儿 tool 消息
    - 加载后 (调 _sanitize_entries) + build_request_messages 都必须能产生合法 messages
    """
    mgr = MessageManager("sys")
    # 旧数据 (手动 mock load_from_disk 后的 _entries)
    mgr._entries.extend([
        {"role": "user", "content": "u1"},
        # 旧版 assistant 写过 tool_calls=[]
        {"role": "assistant", "tool_calls": [], "content": "ok 我先看看"},
        # 孤儿 tool 消息 (assistant 已丢)
        {"role": "tool", "tool_call_id": "ghost", "content": "ghost result"},
        # 正常的助手 + 工具调用 + 工具结果
        {"role": "user", "content": "u2"},
        {"role": "assistant", "tool_calls": [_build_tool_call("L1", "ls", "{}")]},
        {"role": "tool", "tool_call_id": "L1", "content": "out_ls"},
        # 又一个 tool_calls=[] (旧风格)
        {"role": "assistant", "tool_calls": [], "content": "ok 完成了"},
    ])

    # 模拟 load_from_disk 中的清理
    mgr._sanitize_entries()

    # 验证: 清理后 _entries 干净
    for e in mgr._entries:
        if e.get("role") == "assistant":
            assert e.get("tool_calls") != [], (
                f"sanitize 后 assistant 消息不应残留 tool_calls=[], 实际: {e}"
            )
        if e.get("role") == "tool":
            assert e.get("tool_call_id"), (
                f"sanitize 后 tool 消息必须有 tool_call_id, 实际: {e}"
            )

    # 验证: build_request_messages 输出无 DeepSeek 400 风险
    msgs = mgr.build_request_messages("state")

    # 验证: 无 tool_calls=[] 字段
    for m in msgs:
        if m.get("role") == "assistant":
            assert m.get("tool_calls") != [], (
                f"build 输出不应有 tool_calls=[], 实际: {m}"
            )
            if "tool_calls" in m:
                for tc in m["tool_calls"]:
                    assert tc.get("id"), f"tool_call 必须有非空 id, 实际: {tc}"

    # 验证: tool 消息都有匹配的 assistant(tool_calls) 在前面
    seen_tc_ids = set()
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id"):
                    seen_tc_ids.add(tc["id"])
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in seen_tc_ids, (
                f"orphan tool 消息仍在结果中: {m}"
            )

    print("  ✓ test_old_session_simulation")


# ─── Runner ────────────────────────────────────────────


def _run_all():
    tests = [
        test_strip_empty_tool_calls_in_assistant,
        test_drop_orphan_tool_message,
        test_drop_tool_message_with_empty_id,
        test_preserve_valid_assistant_tool_pair,
        test_preserve_multiple_tool_calls_in_one_assistant,
        test_dedup_of_write_markdown_draft,
        test_dedup_does_not_create_orphan,
        test_sanitize_strips_empty_tool_calls,
        test_sanitize_drops_orphan_tool,
        test_sanitize_drops_empty_tool_call_id,
        test_sanitize_preserves_valid_pair,
        test_old_session_simulation,
    ]
    print(f"Running {len(tests)} tests...\n")
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"  ✗ {t.__name__}: {e}")
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(tests)} FAILED")
        for name, err in failed:
            print(f"   - {name}: {err}")
        sys.exit(1)
    else:
        print(f"✅ All {len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
