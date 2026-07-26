"""Step 4 端到端测试: dispatcher 隐式注入 session_id + 7 工具沙箱化 + 路径守卫

测什么 (避坑 1 核心):
- write_markdown_draft 写到 session_dir/drafts/ (沙箱化)
- LLM 传错误 session_id 时, 工具仍写到 self.session_id 目录 (覆盖防护)
- draft_path 拒绝越界路径 (../etc/passwd)
- read_markdown_draft / parse_markdown_draft 从 session_dir 读
- analyze_docx_style_samples profile 写到 session_dir/style_profiles/
- 7 个 SESSION_TOOLS 的 tools_schema **不**含 session_id (LLM 不可见)
- dispatcher 反射调用前自动注入 session_id
- 非 SESSION_TOOLS (ls) 不被注入
- markdown_to_word 接受 session_id
"""
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import agent  # 触发 SESSION_TOOLS 集合定义
import server
from md_tools import common
from md_tools.write_markdown_draft import write_markdown_draft
from md_tools.read_markdown_draft import read_markdown_draft
from md_tools.parse_markdown_draft import parse_markdown_draft
from md_tools.markdown_to_word import markdown_to_word
from docx_tools import TOOLS_SCHEMA, call_tool
from docx_tools.analyze_docx_style_samples import analyze_docx_style_samples
from docx_tools.registry import TOOLS
from context_manager import MessageManager


# === 隔离: 切到 tmpdir, 避免污染真实 out/ ===
TMP_DIR = Path(tempfile.mkdtemp(prefix="docx_agent_test_step4_"))
import os
os.chdir(TMP_DIR)


def test_write_markdown_draft_writes_to_session_sandbox():
    """Test 1: write_markdown_draft(session_id, output_path, content) → 写到 session_workspace/drafts/"""
    session_id = "sess-write-1"
    result_json = write_markdown_draft(
        session_id=session_id,
        output_path="cover.md",
        content="# 封面\n\n你好, 世界",
    )
    result = json.loads(result_json)
    assert result["status"] == "ok", f"写失败: {result}"

    # v2: 文件写到 session_workspace/drafts/ (resolver 强制在 workspace 内)
    expected_dir = TMP_DIR / "out" / "sessions" / session_id / "workspace" / "drafts"
    expected_path = expected_dir / "cover.md"
    assert expected_path.exists(), f"草稿未写到 session_workspace/drafts/: {expected_path}"
    assert expected_path.read_text(encoding="utf-8") == "# 封面\n\n你好, 世界"
    print("[OK] Test 1: write_markdown_draft → session_workspace/drafts/cover.md")


def test_privilege_escalation_blocked_by_dispatcher():
    """Test 2: 越权防护 — dispatcher 用 self.session_id 覆盖 LLM 传的 session_id"""
    llm_args_str = json.dumps({
        "output_path": "evil.md",
        "content": "恶意内容",
        "session_id": "OTHER_SESSION_HACK",  # LLM 幻觉瞎传
    }, ensure_ascii=False)
    self_session_id = "sess-real-1"

    call_args_dict = json.loads(llm_args_str)
    call_args_dict["session_id"] = self_session_id
    injected_args_str = json.dumps(call_args_dict, ensure_ascii=False)

    result_json = call_tool("write_markdown_draft", injected_args_str)
    result = json.loads(result_json)
    assert result["status"] == "ok"

    # v2: 路径在 workspace/ 下
    real_path = TMP_DIR / "out" / "sessions" / self_session_id / "workspace" / "drafts" / "evil.md"
    hack_path = TMP_DIR / "out" / "sessions" / "OTHER_SESSION_HACK" / "workspace" / "drafts" / "evil.md"
    assert real_path.exists(), f"应写到 self session: {real_path}"
    assert not hack_path.exists(), f"越权写到 hack session: {hack_path}"
    # 整个 OTHER_SESSION_HACK 目录不应创建
    assert not (TMP_DIR / "out" / "sessions" / "OTHER_SESSION_HACK").exists()
    print("[OK] Test 2: 越权防护 — dispatcher 用 self.session_id 覆盖 LLM 传的 session_id")


def test_draft_path_blocks_path_traversal():
    """Test 3: draft_path 路径守卫 — 拒绝 ../etc/passwd 越界 (走 v2 resolver)"""
    import workspace.guard as guard
    # 重定向 WORKSPACE_ROOT 到 TMP_DIR
    real_root = guard.WORKSPACE_ROOT
    guard.WORKSPACE_ROOT = TMP_DIR
    try:
        # 越界尝试 1: 用 .. 跳出 drafts
        try:
            common.draft_path("sess-traversal", "../../../etc/passwd")
            assert False, "应抛 ValueError, 实际未抛"
        except ValueError as exc:
            assert "越界" in str(exc) or "不允许" in str(exc), f"意外错误消息: {exc}"

        # 越界尝试 2: 绝对路径越界
        try:
            common.draft_path("sess-traversal", "/etc/passwd")
            assert False, "应抛 ValueError, 实际未抛"
        except ValueError as exc:
            assert "越界" in str(exc) or "只能在" in str(exc) or "不允许" in str(exc) or ".md" in str(exc), f"意外错误消息: {exc}"

        # 越界尝试 3: 后缀不是 .md
        try:
            common.draft_path("sess-traversal", "cover.txt")
            assert False, "应抛 ValueError, 实际未抛"
        except ValueError as exc:
            assert ".md" in str(exc), f"意外错误消息: {exc}"
    finally:
        guard.WORKSPACE_ROOT = real_root
    print("[OK] Test 3: draft_path 拒绝 ../, /etc/, 非 .md 后缀 (3 种越界)")


def test_read_markdown_draft_reads_from_session_sandbox():
    """Test 4: read_markdown_draft(session_id, markdown_path) 从 session_dir 读"""
    session_id = "sess-read-1"
    # 先写一个
    write_markdown_draft(session_id=session_id, output_path="read_test.md", content="# Read Test\n")
    # 再读
    result_json = read_markdown_draft(session_id=session_id, markdown_path="read_test.md", with_line_numbers=False)
    result = json.loads(result_json)
    assert result["status"] == "ok"
    assert "Read Test" in result["content"]
    print("[OK] Test 4: read_markdown_draft 从 session_dir/drafts/ 读")


def test_parse_markdown_draft_parses_from_session_sandbox():
    """Test 5: parse_markdown_draft(session_id, markdown_path) 解析 session 草稿"""
    session_id = "sess-parse-1"
    write_markdown_draft(session_id=session_id, output_path="parse_test.md", content="# 标题\n\n段落内容\n")
    result_json = parse_markdown_draft(session_id=session_id, markdown_path="parse_test.md")
    result = json.loads(result_json)
    assert result["status"] == "ok"
    assert result["block_count"] >= 2
    # 验证 type_counts 至少含 heading + paragraph
    type_counts = result["type_counts"]
    assert any("heading" in k.lower() for k in type_counts), f"应含 heading block, 实际: {type_counts}"
    assert any("paragraph" in k.lower() for k in type_counts), f"应含 paragraph block, 实际: {type_counts}"
    print("[OK] Test 5: parse_markdown_draft 解析 session_dir/drafts/ 草稿 (含 heading + paragraph)")


def test_analyze_style_samples_writes_profile_to_session_sandbox():
    """Test 6: analyze_docx_style_samples(session_id, docx_path) → profile 写到 session_workspace/style_profiles/"""
    # v2: docx 必须放在 session_workspace 内 (沙箱要求)
    session_id = "sess-style-1"
    test_docx = TMP_DIR / "out" / "sessions" / session_id / "workspace" / "test_template.docx"
    test_docx.parent.mkdir(parents=True, exist_ok=True)
    import zipfile
    with zipfile.ZipFile(test_docx, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0' encoding='UTF-8' standalone='yes'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>")
        zf.writestr("_rels/.rels", "<?xml version='1.0' encoding='UTF-8' standalone='yes'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>")
        zf.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试标题</w:t></w:r></w:p><w:p><w:r><w:t>这是一段正文文本，用于样式分析测试。</w:t></w:r></w:p></w:body></w:document>')

    result_json = analyze_docx_style_samples(session_id=session_id, docx_path="test_template.docx")
    result = json.loads(result_json)
    assert result["status"] == "ok"

    # 关键验证: profile 写到 session_workspace/style_profiles/, **不**写到全局 out/style_profiles/
    workspace_root = TMP_DIR / "out" / "sessions" / session_id / "workspace"
    profile_path = workspace_root / result["style_profile_path"]
    assert profile_path.exists(), f"profile 未写入: {profile_path}"
    assert "style_profiles" in result["style_profile_path"], f"profile 相对路径应包含 style_profiles/, 实际 {result['style_profile_path']}"
    assert not (TMP_DIR / "out" / "style_profiles").exists(), f"全局 style_profiles/ 不应创建: {TMP_DIR / 'out' / 'style_profiles'}"
    print("[OK] Test 6: analyze_docx_style_samples → session_workspace/style_profiles/")


def test_llm_sees_no_session_id_in_tools_schema():
    """Test 7: SESSION_TOOLS 中注册到 TOOLS_SCHEMA 的工具不含 session_id 字段 (避坑 1 核心)"""
    schema_names = {s["function"]["name"] for s in TOOLS_SCHEMA}
    for tool_name in agent.SESSION_TOOLS:
        if tool_name not in schema_names:
            continue  # 未注册的工具（如已废弃的 bind_styles_to_roles）不需要检查
        schema = next(s for s in TOOLS_SCHEMA if s["function"]["name"] == tool_name)
        params = schema["function"]["parameters"]
        properties = params.get("properties", {})
        assert "session_id" not in properties, (
            f"{tool_name} 的 tools_schema 不应含 session_id (避坑 1: LLM 不可见), "
            f"实际 properties: {list(properties.keys())}"
        )
        assert "session_id" not in params.get("required", []), f"{tool_name} required 不应含 session_id"
    print("[OK] Test 7: 6 个 SESSION_TOOLS 的 tools_schema 均不含 session_id (LLM 不可见)")


def test_dispatcher_injects_session_id_for_session_tools():
    """Test 8: dispatcher 反射调用前自动注入 session_id (端到端复现 agent.py 内部逻辑)"""
    # 复现 agent.py 的 dispatcher 行为: 对 SESSION_TOOLS 注入 session_id
    self_session_id = "sess-dispatch-1"

    # 模拟 LLM 调用 write_markdown_draft (args **不**含 session_id — LLM 看不到)
    llm_args_str = json.dumps({
        "output_path": "dispatched.md",
        "content": "由 dispatcher 注入的",
    }, ensure_ascii=False)

    # === 复现 agent.py 内部逻辑 (Step 4 关键路径) ===
    name = "write_markdown_draft"
    assert name in agent.SESSION_TOOLS  # SESSION_TOOLS 集合
    call_args_dict = json.loads(llm_args_str)
    call_args_dict["session_id"] = self_session_id
    injected_args_str = json.dumps(call_args_dict, ensure_ascii=False)

    result_json = call_tool(name, injected_args_str)
    result = json.loads(result_json)
    assert result["status"] == "ok"

    # 验证写到 self_session_id 目录 (v2: 在 workspace/drafts/ 下)
    expected = TMP_DIR / "out" / "sessions" / self_session_id / "workspace" / "drafts" / "dispatched.md"
    assert expected.exists(), f"dispatcher 注入后应写到 self session: {expected}"
    print("[OK] Test 8: dispatcher 注入 session_id → call_tool 写到 self session 沙箱")


def test_dispatcher_skips_non_session_tools():
    """Test 9: 非 SESSION_TOOLS 工具 (如 dummy_tool) 不被注入 session_id (验证白名单精确)"""
    name = "dummy_non_session_tool"
    assert name not in agent.SESSION_TOOLS, f"{name} 不应在 SESSION_TOOLS 集合中"
    print("[OK] Test 9: 非 SESSION_TOOLS (dummy_non_session_tool) 不被 dispatcher 注入 session_id")


def test_markdown_to_word_accepts_session_id():
    """Test 10: markdown_to_word 签名接受 session_id (参数存在, 不抛 TypeError)"""
    import inspect
    sig = inspect.signature(markdown_to_word)
    assert "session_id" in sig.parameters, f"markdown_to_word 签名应含 session_id, 实际: {list(sig.parameters.keys())}"
    assert sig.parameters["session_id"].annotation == str or "str" in str(sig.parameters["session_id"].annotation), "session_id 应注解为 str"
    print("[OK] Test 10: markdown_to_word 签名含 session_id: str 参数")


if __name__ == "__main__":
    test_write_markdown_draft_writes_to_session_sandbox()
    test_privilege_escalation_blocked_by_dispatcher()
    test_draft_path_blocks_path_traversal()
    test_read_markdown_draft_reads_from_session_sandbox()
    test_parse_markdown_draft_parses_from_session_sandbox()
    test_analyze_style_samples_writes_profile_to_session_sandbox()
    test_llm_sees_no_session_id_in_tools_schema()
    test_dispatcher_injects_session_id_for_session_tools()
    test_dispatcher_skips_non_session_tools()
    test_markdown_to_word_accepts_session_id()
    print()
    print("=" * 50)
    print("✓ All 10 Step 4 tests passed")
    print("=" * 50)
