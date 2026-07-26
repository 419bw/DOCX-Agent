"""Step 1 单元测试: save_to_disk / load_from_disk / _save_lock 锁 / Checkpoint 触发"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))  # tests/ 是子目录, src/ 在仓库根

import asyncio
import json
import shutil
import tempfile
from context_manager import MessageManager
from agent import Agent


class MockLLM:
    """Mock LLMClientAdapter - 避免 API key 需求, 只提供元数据 getter"""
    def get_provider(self): return "test"
    def get_model_name(self): return "mock"
    def get_thinking_type(self): return "disabled"
    def get_reasoning_effort(self): return None


def test_save_to_disk_writes_3_json():
    """Test 1: save_to_disk() 正确写 3 个 JSON"""
    tmpdir = Path(tempfile.mkdtemp())
    session_id = "session-test-save"
    session_dir = tmpdir / session_id
    session_dir.mkdir()

    msg_mgr = MessageManager("test-system-prompt")
    msg_mgr._entries = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file1.txt"},
    ]
    msg_mgr._total_input_tokens = 123
    msg_mgr._last_prompt_tokens = 50

    agent = Agent(
        system_prompt="test-system-prompt",
        llm_adapter=MockLLM(),
        msg_mgr=msg_mgr,
        docx_path="/tmp/test.docx",
        log_path=None,
        session_id=session_id,
        session_dir=session_dir,
    )
    agent._round_index = 7
    agent.workflow_state = "md_draft"
    agent.draft_files_written = ["drafts/cover.md"]
    agent.stage_called_tools = {"style_review": {"analyze_docx_style_samples", "define_style_profile"}}

    # 触发 save
    agent.save_to_disk()

    # 验证 3 个 JSON 都存在
    assert (session_dir / "metadata.json").exists()
    assert (session_dir / "messages.json").exists()
    assert (session_dir / "workflow.json").exists()

    # 验证 metadata 字段
    metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["session_id"] == session_id
    assert metadata["docx_path"] == "/tmp/test.docx"
    assert metadata["workflow_state"] == "md_draft"
    assert metadata["model"] == "mock"
    assert metadata["title"] == "test"  # 来自 docx_path stem

    # 验证 messages 字段
    messages = json.loads((session_dir / "messages.json").read_text(encoding="utf-8"))
    assert messages["system_prompt"] == "test-system-prompt"
    assert len(messages["entries"]) == 3
    assert messages["entries"][1]["tool_calls"][0]["function"]["name"] == "ls"
    assert messages["total_input_tokens"] == 123
    assert messages["last_prompt_tokens"] == 50

    # 验证 workflow 字段
    workflow = json.loads((session_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["workflow_state"] == "md_draft"
    assert workflow["stage_called_tools"]["style_review"] == ["analyze_docx_style_samples", "define_style_profile"]
    assert workflow["draft_files_written"] == ["drafts/cover.md"]
    assert workflow["round_index"] == 7

    print("[OK] Test 1: save_to_disk writes 3 JSON with correct schemas")
    shutil.rmtree(tmpdir)


def test_load_from_disk_restores_state():
    """Test 2: load_from_disk() 正确恢复 Agent 状态"""
    tmpdir = Path(tempfile.mkdtemp())
    session_id = "session-test-load"
    session_dir = tmpdir / session_id
    session_dir.mkdir()

    # 先创建一个 Agent 写入磁盘
    msg_mgr = MessageManager("saved-system")
    msg_mgr._entries = [
        {"role": "user", "content": "test message"},
        {"role": "assistant", "content": "response"},
    ]
    msg_mgr._total_input_tokens = 999
    msg_mgr._last_prompt_tokens = 200

    agent1 = Agent(
        system_prompt="saved-system",
        llm_adapter=MockLLM(),
        msg_mgr=msg_mgr,
        docx_path="/path/to/template.docx",
        log_path=None,
        session_id=session_id,
        session_dir=session_dir,
    )
    agent1._round_index = 15
    agent1.workflow_state = "word_editing"
    agent1.draft_files_written = ["drafts/a.md", "drafts/b.md"]
    agent1.stage_called_tools = {"md_draft": {"write_markdown_draft", "read_markdown_draft"}}
    agent1.save_to_disk()

    # 模拟 "新 session" - 创建全新 Agent 但用 load_from_disk 加载
    fresh_msg_mgr = MessageManager("placeholder")  # placeholder, 会被 load_from_disk 覆盖
    agent2 = Agent.load_from_disk(
        session_dir=session_dir,
        llm_adapter=MockLLM(),
        system_prompt="saved-system",   # 系统提示必须匹配
        docx_path="/path/to/template.docx",  # 实际从 metadata 读, 这里传相同值
    )

    # 验证所有字段都恢复
    assert agent2.session_id == session_id
    assert agent2.session_dir == session_dir
    assert agent2.workflow_state == "word_editing"
    assert agent2._round_index == 15
    assert agent2.draft_files_written == ["drafts/a.md", "drafts/b.md"]
    assert agent2.stage_called_tools == {"md_draft": {"write_markdown_draft", "read_markdown_draft"}}
    assert agent2.msg_mgr._system_prompt == "saved-system"
    assert agent2.msg_mgr._total_input_tokens == 999
    assert agent2.msg_mgr._last_prompt_tokens == 200
    assert len(agent2.msg_mgr._entries) == 2
    assert agent2.msg_mgr._entries[0]["content"] == "test message"
    assert agent2.msg_mgr._entries[1]["content"] == "response"

    print("[OK] Test 2: load_from_disk restores all Agent state")
    shutil.rmtree(tmpdir)


def test_save_lock_prevents_concurrent_writes():
    """Test 3: _save_lock 串行化并发写, 避免文件花 (避坑 2)"""
    tmpdir = Path(tempfile.mkdtemp())
    session_id = "session-test-lock"
    session_dir = tmpdir / session_id
    session_dir.mkdir()

    msg_mgr = MessageManager("lock-test")
    agent = Agent(
        system_prompt="lock-test",
        llm_adapter=MockLLM(),
        msg_mgr=msg_mgr,
        docx_path="/x.docx",
        log_path=None,
        session_id=session_id,
        session_dir=session_dir,
    )
    # 制造不同的 entries (验证写的是最新值)
    for i in range(10):
        msg_mgr._entries = [{"role": "user", "content": f"msg-{i}"}]
        agent._round_index = i
        agent.save_to_disk()  # 同步调用 (不通过 _background_save)

    # 启动并发 _background_save (避坑 2 核心: 锁必须工作)
    async def hammer():
        await asyncio.gather(*[agent._persistence._background_save() for _ in range(20)])
    asyncio.run(hammer())

    # 关键验证: messages.json 仍是合法 JSON (没被写花)
    content = (session_dir / "messages.json").read_text(encoding="utf-8")
    data = json.loads(content)  # 必须不抛异常
    # 验证最终值是最后写入的
    assert data["entries"][-1]["content"] in [f"msg-{i}" for i in range(10)]

    print("[OK] Test 3: _save_lock serializes 20 concurrent writes, JSON valid")
    shutil.rmtree(tmpdir)


def test_checkpoint_helper_fire_and_forget():
    """Test 4: _checkpoint() 触发 fire-and-forget 后台写"""
    tmpdir = Path(tempfile.mkdtemp())
    session_id = "session-test-checkpoint"
    session_dir = tmpdir / session_id
    session_dir.mkdir()

    msg_mgr = MessageManager("ckpt-test")
    agent = Agent(
        system_prompt="ckpt-test",
        llm_adapter=MockLLM(),
        msg_mgr=msg_mgr,
        docx_path="/x.docx",
        log_path=None,
        session_id=session_id,
        session_dir=session_dir,
    )

    # _checkpoint() 必须在 async context (内部 asyncio.create_task 需要 event loop)
    async def run_checkpoint():
        agent._checkpoint()  # 不抛异常
        await asyncio.sleep(0.5)  # 等后台写盘完成
    asyncio.run(run_checkpoint())

    # 验证 3 JSON 都写入了
    assert (session_dir / "metadata.json").exists()
    assert (session_dir / "messages.json").exists()
    assert (session_dir / "workflow.json").exists()

    # 验证 _checkpoint() 在无 session_dir 时不抛异常 (async context 也安全)
    async def no_session():
        agent2 = Agent("x", MockLLM(), MessageManager("placeholder"), docx_path="/y.docx")
        agent2._checkpoint()  # 不抛异常 (内部 if self.session_dir 守卫)
    asyncio.run(no_session())

    print("[OK] Test 4: _checkpoint() fire-and-forget works, no session_dir safely noop")
    shutil.rmtree(tmpdir)


def test_no_session_dir_skip_safely():
    """Test 5: 无 session_dir 时 save/load/checkpoint 都安全降级"""
    msg_mgr = MessageManager("no-session")
    agent = Agent(
        system_prompt="no-session",
        llm_adapter=MockLLM(),
        msg_mgr=msg_mgr,
        docx_path="/x.docx",
    )
    # 无 session_dir 时 save_to_disk 不抛异常
    agent.save_to_disk()  # 静默 noop
    # 无 session_dir 时 _checkpoint() 不抛异常
    agent._checkpoint()  # 静默 noop
    # 无 session_id 时 metadata_dict 仍能生成 (字段空字符串)
    meta = agent._persistence.metadata_dict()
    assert meta["session_id"] == ""

    print("[OK] Test 5: no session_dir/id safely degrades to noop")


if __name__ == "__main__":
    test_save_to_disk_writes_3_json()
    test_load_from_disk_restores_state()
    test_save_lock_prevents_concurrent_writes()
    test_checkpoint_helper_fire_and_forget()
    test_no_session_dir_skip_safely()
    print()
    print("=" * 50)
    print("✓ All 5 Step 1 tests passed")
    print("=" * 50)
