"""detail_edit 模式集成测试: 初始化 + 工具选用 + request_more_tools + done + 持久化"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncio
import json
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from context_manager import MessageManager
from agent import Agent
from prompts import DETAIL_EDIT, DETAIL_EDIT_BASE_TOOLS


# === Mock LLM ===

class MockLLM:
    """Mock LLMClientAdapter"""
    def __init__(self):
        self._blocking_response = "[]"

    def create_chat_completion_blocking(self, *, messages, tools=None, **kwargs):
        choice = MagicMock()
        choice.message.content = self._blocking_response
        response = MagicMock()
        response.choices = [choice]
        return response

    def create_chat_completion(self, messages, tools=None, **kwargs):
        """流式: 返回一个 chunk 迭代器"""
        chunk = MagicMock()
        chunk.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        delta = MagicMock()
        delta.content = "编辑完成。"
        delta.reasoning_content = None
        delta.tool_calls = None
        delta.model_extra = None
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.delta = delta
        chunk.choices = [choice]
        return [chunk]

    def get_provider(self): return "test"
    def get_model_name(self): return "mock"
    def get_thinking_type(self): return "disabled"
    def get_reasoning_effort(self): return None

    @property
    def reasoning_field(self): return "delta.reasoning_content"

    @property
    def quirks(self): return ()


def _make_agent(mode="fill", tmpdir=None, **kwargs):
    """构造测试 Agent"""
    if tmpdir is None:
        tmpdir = Path(tempfile.mkdtemp())
    session_id = "session-test-detail"
    session_dir = tmpdir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    msg_mgr = MessageManager("test-system-prompt")
    msg_mgr.append_user("把第三章标题改成'实验结果分析'")

    llm = MockLLM()
    agent = Agent(
        system_prompt="test-system-prompt",
        llm_adapter=llm,
        msg_mgr=msg_mgr,
        docx_path="test.docx",
        log_path=None,
        session_id=session_id,
        session_dir=session_dir,
        mode=mode,
        **kwargs,
    )
    return agent, tmpdir, llm


# === 初始化 ===

def test_agent_mode_default_fill():
    """默认模式是 fill"""
    agent, tmpdir, _ = _make_agent()
    assert agent.mode == "fill"
    shutil.rmtree(tmpdir)


def test_agent_mode_detail_edit():
    """可以设置 detail_edit 模式"""
    agent, tmpdir, _ = _make_agent(mode="detail_edit")
    assert agent.mode == "detail_edit"
    assert agent.selected_tools == set()
    assert agent._doc_structure_cache == ""
    shutil.rmtree(tmpdir)


# === 工具选用事件序列 ===

def test_detail_edit_tool_selection_events():
    """detail_edit 首次 step() 产生 tool_selection_start/end 事件"""
    agent, tmpdir, llm = _make_agent(mode="detail_edit")
    llm._blocking_response = '["replace_text", "set_text_format"]'

    events = []
    async def run():
        async for event in agent.step():
            events.append(event)
            # 只收集到 tool_selection_end 就停 (后面会进 LLM 循环)
            if event.get("type") == "tool_selection_end":
                break

    asyncio.run(run())

    types = [e["type"] for e in events]
    assert "tool_selection_start" in types
    assert "tool_selection_end" in types

    # tool_selection_end 应包含 selected_tools
    end_event = next(e for e in events if e["type"] == "tool_selection_end")
    assert "selected_tools" in end_event
    assert "replace_text" in end_event["selected_tools"]
    assert "set_text_format" in end_event["selected_tools"]
    # 基础工具也在
    for base in DETAIL_EDIT_BASE_TOOLS:
        assert base in end_event["selected_tools"]

    shutil.rmtree(tmpdir)


# === request_more_tools ===

def test_handle_request_more_tools():
    """_handle_request_more_tools 扩充工具集"""
    agent, tmpdir, llm = _make_agent(mode="detail_edit")
    agent.selected_tools = {"replace_text"} | DETAIL_EDIT_BASE_TOOLS
    agent._doc_structure_cache = '{"paragraphs": []}'
    llm._blocking_response = '["insert_paragraph_after"]'

    result_str = asyncio.run(agent._mode._handle_request_more_tools(
        agent, json.dumps({"reason": "需要插入新段落"})
    ))
    result = json.loads(result_str)

    assert result["status"] == "ok"
    assert "insert_paragraph_after" in result["added_tools"]
    # 旧工具不移除
    assert "replace_text" in agent.selected_tools
    assert "insert_paragraph_after" in agent.selected_tools

    shutil.rmtree(tmpdir)


# === done 逻辑 ===

def test_detail_edit_done_on_content():
    """detail_edit 模式: 无工具调用 + 有意义内容 → done"""
    agent, tmpdir, llm = _make_agent(mode="detail_edit")
    agent.selected_tools = {"replace_text"} | DETAIL_EDIT_BASE_TOOLS
    agent._doc_structure_cache = "{}"

    events = []
    async def run():
        async for event in agent.step():
            events.append(event)
            if event.get("type") == "done":
                break

    asyncio.run(run())

    types = [e["type"] for e in events]
    assert "done" in types
    # v3: detail_edit done 后 workflow_state 保持 DETAIL_EDIT (不是 "done"), 以支持 resume 续编
    assert agent.workflow_state != "done"

    shutil.rmtree(tmpdir)


# === done 后 resume 保持上下文 (Bug #8 回归) ===

def test_detail_edit_done_then_resume():
    """回归: detail_edit done 后 resume, mode/selected_tools/消息历史完整恢复"""
    tmpdir = Path(tempfile.mkdtemp())
    agent, _, llm = _make_agent(mode="detail_edit", tmpdir=tmpdir)
    agent.selected_tools = {"replace_text"} | DETAIL_EDIT_BASE_TOOLS
    agent._doc_structure_cache = '{"test": true}'

    # 跑一轮 done
    async def run():
        async for event in agent.step():
            if event.get("type") == "done":
                break
    asyncio.run(run())
    agent.save_to_disk()

    # resume: 从磁盘恢复
    restored = Agent.load_from_disk(
        session_dir=agent.session_dir,
        llm_adapter=llm,
        system_prompt="test-system-prompt",
        docx_path="test.docx",
    )
    assert restored.mode == "detail_edit"
    assert "replace_text" in restored.selected_tools
    assert restored._doc_structure_cache == '{"test": true}'
    # workflow_state 不是 "done", resume 后 step() 不会直接 return
    assert restored.workflow_state != "done"
    # 消息历史保留
    assert len(restored.msg_mgr._entries) > 0

    shutil.rmtree(tmpdir)


# === request_more_tools yield tool_selection_end (Bug #9 回归) ===

def test_request_more_tools_yields_tool_selection_end():
    """回归: request_more_tools 后 step() 事件流包含 tool_selection_end"""
    agent, tmpdir, llm = _make_agent(mode="detail_edit")
    agent.selected_tools = {"replace_text"} | DETAIL_EDIT_BASE_TOOLS
    agent._doc_structure_cache = "{}"

    # mock LLM: 第一轮返回 request_more_tools 工具调用, 第二轮返回纯文本 done
    call_count = [0]
    original_create = llm.create_chat_completion
    def mock_stream(messages, tools=None, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # 第一轮: 返回 request_more_tools 工具调用
            chunk = MagicMock()
            chunk.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
            delta = MagicMock()
            delta.content = None
            delta.reasoning_content = None
            delta.model_extra = None
            tc = MagicMock()
            tc.index = 0
            tc.id = "call_rmt_1"
            tc.function.name = "request_more_tools"
            tc.function.arguments = '{"reason": "需要插入图片"}'
            delta.tool_calls = [tc]
            choice = MagicMock()
            choice.finish_reason = "tool_calls"
            choice.delta = delta
            chunk.choices = [choice]
            return [chunk]
        else:
            # 第二轮: 纯文本 done
            return original_create(messages, tools, **kwargs)

    llm.create_chat_completion = mock_stream
    llm._blocking_response = '["insert_image_after_paragraph"]'

    events = []
    async def run():
        async for event in agent.step():
            events.append(event)
            if event.get("type") == "done":
                break
    asyncio.run(run())

    types = [e["type"] for e in events]
    assert "tool_selection_end" in types, f"事件流中应有 tool_selection_end, 实际: {types}"
    # tool_selection_end 应包含扩充后的工具列表
    tse = next(e for e in events if e["type"] == "tool_selection_end")
    assert "insert_image_after_paragraph" in tse["selected_tools"]

    shutil.rmtree(tmpdir)


# === 持久化 + 恢复 ===

def test_detail_edit_persistence():
    """detail_edit 模式持久化 + 恢复"""
    tmpdir = Path(tempfile.mkdtemp())
    agent, _, llm = _make_agent(mode="detail_edit", tmpdir=tmpdir)
    agent.selected_tools = {"replace_text", "set_text_format"} | DETAIL_EDIT_BASE_TOOLS
    agent._doc_structure_cache = '{"test": true}'
    agent.workflow_state = DETAIL_EDIT

    agent.save_to_disk()

    # 验证 metadata 含 mode
    meta = json.loads((agent.session_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "detail_edit"

    # 验证 workflow 含 selected_tools
    wf = json.loads((agent.session_dir / "workflow.json").read_text(encoding="utf-8"))
    assert "replace_text" in wf["selected_tools"]
    assert wf["doc_structure_cache"] == '{"test": true}'

    # 恢复
    restored = Agent.load_from_disk(
        session_dir=agent.session_dir,
        llm_adapter=llm,
        system_prompt="test-system-prompt",
        docx_path="test.docx",
    )
    assert restored.mode == "detail_edit"
    assert "replace_text" in restored.selected_tools
    assert restored._doc_structure_cache == '{"test": true}'

    shutil.rmtree(tmpdir)


# === fill 模式回归 ===

def test_fill_mode_not_affected():
    """fill 模式不受 detail_edit 改动影响"""
    agent, tmpdir, _ = _make_agent(mode="fill")
    assert agent.mode == "fill"
    assert agent.workflow_state == "style_review"
    assert agent.selected_tools == set()
    shutil.rmtree(tmpdir)
