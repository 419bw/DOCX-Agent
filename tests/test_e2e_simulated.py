"""端到端模拟测试: 完整走通 Agent → Strategy → LLM → 工具 → 事件流。

MockLLM 按轮次返回预设响应（不调真实 API），但工具调用走真实 call_tool。
验证两种模式的完整事件序列和最终状态。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncio
import json
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

pytest_plugins = ["_docx_factory"]

from context_manager import MessageManager
from agent import Agent
from prompts import DETAIL_EDIT, STYLE_REVIEW, MD_DRAFT, WORD_EDITING
from _docx_factory import _build_minimal_docx, _build_full_docx


# =====================================================================
# ScriptedMockLLM: 按调用次序返回预设响应
# =====================================================================

def _make_tool_chunk(tool_name, tool_args, call_id="call_01"):
    """构造一个含 tool_call 的流式 chunk 列表"""
    chunk = MagicMock()
    chunk.usage = MagicMock(prompt_tokens=50, completion_tokens=20)
    delta = MagicMock()
    delta.content = None
    delta.reasoning_content = None
    delta.model_extra = None
    tc = MagicMock()
    tc.index = 0
    tc.id = call_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(tool_args, ensure_ascii=False)
    delta.tool_calls = [tc]
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.delta = delta
    chunk.choices = [choice]
    return [chunk]


def _make_text_chunk(text):
    """构造一个含纯文本的流式 chunk 列表"""
    chunk = MagicMock()
    chunk.usage = MagicMock(prompt_tokens=50, completion_tokens=20)
    delta = MagicMock()
    delta.content = text
    delta.reasoning_content = None
    delta.tool_calls = None
    delta.model_extra = None
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.delta = delta
    chunk.choices = [choice]
    return [chunk]


class ScriptedMockLLM:
    """按调用次序返回预设响应的 Mock LLM。

    scripts: list of chunk lists, 每次 create_chat_completion 消耗一个。
    blocking_scripts: 工具选用 agent 的阻塞调用响应。
    """
    def __init__(self, scripts, blocking_scripts=None):
        self._scripts = list(scripts)
        self._blocking_scripts = list(blocking_scripts or [])
        self._call_index = 0
        self._blocking_index = 0

    def create_chat_completion(self, messages, tools=None, **kwargs):
        if self._call_index < len(self._scripts):
            result = self._scripts[self._call_index]
            self._call_index += 1
            return result
        return _make_text_chunk("（无更多预设响应）")

    def create_chat_completion_blocking(self, *, messages, tools=None, **kwargs):
        if self._blocking_index < len(self._blocking_scripts):
            content = self._blocking_scripts[self._blocking_index]
            self._blocking_index += 1
        else:
            content = "[]"
        choice = MagicMock()
        choice.message.content = content
        response = MagicMock()
        response.choices = [choice]
        return response

    def get_provider(self): return "test"
    def get_model_name(self): return "scripted-mock"
    def get_thinking_type(self): return "disabled"
    def get_reasoning_effort(self): return None

    @property
    def reasoning_field(self): return "delta.reasoning_content"

    @property
    def quirks(self): return ()


# =====================================================================
# detail_edit 端到端: 工具选用 → ls → replace_text → 预览 → done → resume
# =====================================================================

def test_detail_edit_full_e2e(tmp_root, session_id):
    """detail_edit 完整流程: 4 轮 LLM + 工具调用 + 预览事件 + done"""
    ws = tmp_root / session_id / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    _build_minimal_docx(ws / "test.docx", ["实验目的：验证假设", "实验步骤：按流程操作"])

    # 预设 LLM 响应序列:
    # Round 1: 调 ls
    # Round 2: 调 replace_text (同路径)
    # Round 3: 调 diff_docx
    # Round 4: 纯文本总结 → done
    scripts = [
        _make_tool_chunk("ls", {}),
        _make_tool_chunk("replace_text", {
            "docx_path": "test.docx", "output_path": "test.docx",
            "old_text": "实验目的", "new_text": "实验目标",
        }),
        _make_tool_chunk("diff_docx", {
            "before_docx": "test.docx", "after_docx": "test.docx",
        }),
        _make_text_chunk("已将文档中的'实验目的'替换为'实验目标'，diff 验证通过。"),
    ]
    # 工具选用 agent 的阻塞响应
    blocking_scripts = ['["replace_text"]']

    llm = ScriptedMockLLM(scripts, blocking_scripts)
    session_dir = tmp_root / session_id
    msg_mgr = MessageManager("test-system-prompt")
    msg_mgr.append_user("把文档中的'实验目的'替换为'实验目标'")

    agent = Agent(
        system_prompt="test-system-prompt", llm_adapter=llm,
        msg_mgr=msg_mgr, docx_path="test.docx",
        session_id=session_id, session_dir=session_dir,
        mode="detail_edit",
    )

    events = []
    async def run():
        async for event in agent.step():
            events.append(event)
    asyncio.run(run())

    types = [e["type"] for e in events]

    # 验证事件序列
    assert "tool_selection_start" in types, "应有工具选用开始事件"
    assert "tool_selection_end" in types, "应有工具选用结束事件"
    assert types.count("round_start") == 4, f"应有 4 轮 round_start, 实际 {types.count('round_start')}"
    assert types.count("tool_start") == 3, "应有 3 次工具调用 (ls + replace_text + diff_docx)"
    assert types.count("tool_end") == 3
    assert "docx_preview_ready" in types, "replace_text 后应有预览事件"
    assert types[-1] == "done", "最后应是 done"

    # 验证工具选用结果
    tse = next(e for e in events if e["type"] == "tool_selection_end")
    assert "replace_text" in tse["selected_tools"]
    assert "read_docx_structure" in tse["selected_tools"]  # 基础工具

    # 验证 replace_text 真的生效了
    from _docx_factory import get_xml_text
    assert "实验目标" in get_xml_text(ws / "test.docx", "//w:t")

    # 验证预览事件有 diff 数据
    preview = next(e for e in events if e["type"] == "docx_preview_ready")
    assert "test.docx" in preview["preview_path"]
    assert "paragraph_changes" in preview

    # 验证 done 后 workflow_state 不是 "done" (支持 resume)
    assert agent.workflow_state != "done"
    assert agent.mode == "detail_edit"

    # === 持久化 + resume 续编 ===
    agent.save_to_disk()
    restored = Agent.load_from_disk(
        session_dir=session_dir, llm_adapter=llm,
        system_prompt="test-system-prompt", docx_path="test.docx",
    )
    assert restored.mode == "detail_edit"
    assert "replace_text" in restored.selected_tools
    assert len(restored.msg_mgr._entries) > 0, "消息历史应保留"


# =====================================================================
# fill 端到端: 样式审核 → 审批 → 草稿 → 审批 → 写入 → done
# =====================================================================

def test_fill_mode_full_e2e(tmp_root, session_id):
    """fill 完整三阶段流程: 样式审核 → 审批 → 草稿 → 审批 → 写入 → done"""
    ws = tmp_root / session_id / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    _build_full_docx(ws / "template.docx", ["标题占位", "正文占位"])

    # 预设 LLM 响应:
    # R1: 调 analyze_docx_style_samples
    # R2: 调 define_style_profile
    # R3: 纯文本 (样式分析结果) → 触发 wait_approval
    # --- 用户 approve ---
    # R4: 调 write_markdown_draft
    # R5: 纯文本 (草稿完成) → 触发 wait_approval
    # --- 用户 approve ---
    # R6: 调 markdown_to_word (简化: 返回 ok)
    # R7: 纯文本 (写入完成) → done
    scripts = [
        _make_tool_chunk("analyze_docx_style_samples", {"docx_path": "template.docx"}),
        _make_tool_chunk("define_style_profile", {
            "styles": {
                "body": {"format": {"font_east_asia": "宋体"}, "paragraph_format": {}},
            }
        }),
        _make_text_chunk("样式分析完成。建议：标题用 sample_1，正文用 sample_1。请确认。"),
        _make_tool_chunk("write_markdown_draft", {
            "output_path": "cover.md", "content": "# 实验报告\n\n这是填充内容。",
        }),
        _make_text_chunk("草稿已写入 cover.md，请审核。"),
        _make_text_chunk("写入完成，diff 验证通过。"),
    ]

    llm = ScriptedMockLLM(scripts)
    session_dir = tmp_root / session_id
    msg_mgr = MessageManager("test-system-prompt")
    msg_mgr.append_user("填充这个模板")

    agent = Agent(
        system_prompt="test-system-prompt", llm_adapter=llm,
        msg_mgr=msg_mgr, docx_path="template.docx",
        session_id=session_id, session_dir=session_dir,
        mode="fill",
    )

    # 第一阶段: 跑到 wait_approval (样式审核)
    events_phase1 = []
    async def run_phase1():
        async for event in agent.step():
            events_phase1.append(event)
            if event["type"] == "wait_approval":
                break
    asyncio.run(run_phase1())

    types1 = [e["type"] for e in events_phase1]
    assert "tool_start" in types1, "应调用工具"
    assert "wait_approval" in types1, "样式审核后应触发审批"
    assert agent.workflow_state == STYLE_REVIEW

    # 用户 approve → 进入 MD_DRAFT
    agent.on_user_feedback({"type": "approve", "approved": True})
    events_phase2 = []
    async def run_phase2():
        async for event in agent.step():
            events_phase2.append(event)
            if event["type"] == "wait_approval":
                break
    asyncio.run(run_phase2())

    types2 = [e["type"] for e in events_phase2]
    assert agent.workflow_state == MD_DRAFT
    assert "wait_approval" in types2, "草稿后应触发审批"

    # 用户 approve → 进入 WORD_EDITING → done
    agent.on_user_feedback({"type": "approve", "approved": True})
    events_phase3 = []
    async def run_phase3():
        async for event in agent.step():
            events_phase3.append(event)
            if event["type"] == "done":
                break
    asyncio.run(run_phase3())

    types3 = [e["type"] for e in events_phase3]
    assert "done" in types3, "最终应 done"

    # 验证全程工具调用
    all_events = events_phase1 + events_phase2 + events_phase3
    tool_names = [e["name"] for e in all_events if e["type"] == "tool_start"]
    assert "analyze_docx_style_samples" in tool_names
    assert "define_style_profile" in tool_names
    assert "write_markdown_draft" in tool_names
