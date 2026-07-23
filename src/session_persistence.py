"""
DOCX Agent Session 持久化层。

从 src/agent.py 抽出(Step B 重构): 负责把 Agent 状态序列化到 disk
(metadata.json / messages.json / workflow.json 三个 JSON)
+ 异步串行化写盘 (asyncio.Lock + asyncio.to_thread)。

设计要点 (用户补丁 1):
- 用 weakref.ref(agent) 持有 agent 引用, 避免双向循环引用
  (Agent → _persistence → agent 强引用 → GC 滞后)
- 锁 (asyncio.Lock) 移入本类内部, Agent 不再持有
- 暴露 checkpoint() / save_sync() / read_session_files() 三个公开 API
- Agent.load_from_disk classmethod 仍**留在 agent.py**, 内部调 SessionPersistence.read_session_files

边界:
- SessionPersistence 不知道 LLM / 业务逻辑, 只做"读 agent 属性 → 写 JSON" / "读 JSON → 返回 dict"
- Agent 字段访问全部走 self.agent property (解 weakref, 失败时 raise)
"""

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from agent import Agent


class SessionPersistence:
    """Session 持久化层 — 封装 3 个 _dict() + save/load + asyncio.Lock。

    实例由 Agent.__init__ 创建, Agent 持 self._persistence。
    本类对 Agent 持 weakref 引用, Agent 销毁后所有方法 raise RuntimeError。
    """

    def __init__(self, agent: "Agent") -> None:
        self._agent_ref = weakref.ref(agent)
        # 写盘异步锁 (避坑 2: tool_start/tool_end 间隔 < 几毫秒 时的文件写花)
        self._lock = asyncio.Lock()

    # === weakref 解析 ===

    @property
    def agent(self) -> "Agent":
        a = self._agent_ref()
        if a is None:
            raise RuntimeError("Agent instance has been garbage collected")
        return a

    # === 公开 API (Agent 调用) ===

    def checkpoint(self) -> None:
        """5 个 Checkpoint 触发点统一调用: fire-and-forget 后台 save。

        无 session_dir 时 (测试场景) 跳过, 不创建空 task。
        """
        a = self.agent
        if a.session_dir:
            asyncio.create_task(self._background_save())

    def save_sync(self) -> None:
        """同步写盘 (实际 I/O 在 thread) — 序列化 3 个 JSON 到 self.session_dir。

        供 Agent.save_to_disk 公开方法委托调用, 保持向后兼容
        (测试 / 旧调用方 Agent.save_to_disk() 不破)。
        """
        a = self.agent
        if not a.session_dir:
            return
        a.session_dir.mkdir(parents=True, exist_ok=True)
        (a.session_dir / "metadata.json").write_text(
            json.dumps(self.metadata_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (a.session_dir / "messages.json").write_text(
            json.dumps(self.messages_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (a.session_dir / "workflow.json").write_text(
            json.dumps(self.workflow_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 注意: 草稿文件 (.md) / style_profiles/ / uploads/ **不需要** "snapshot" 复制 —
        # 它们从诞生起就在 session_dir/drafts/ / style_profiles/ / uploads/ 下
        # (工具 dispatcher 隐式注入 session_id 派生 session_dir 写入, 避坑 1)

    @staticmethod
    def read_session_files(session_dir: Path) -> Tuple[dict, dict, dict]:
        """读 3 个 JSON — Agent.load_from_disk classmethod 调用。

        返回 (metadata, messages_data, workflow) 三个 dict, 调用方负责构造 Agent。
        """
        metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
        messages_data = json.loads((session_dir / "messages.json").read_text(encoding="utf-8"))
        workflow = json.loads((session_dir / "workflow.json").read_text(encoding="utf-8"))
        return metadata, messages_data, workflow

    # === 私有方法 (background + dict 序列化) ===

    async def _background_save(self) -> None:
        """异步串行化写盘: 同一 session 同一时刻只有一个写盘线程。"""
        a = self.agent
        if not a.session_dir:
            return
        async with self._lock:  # 锁
            await asyncio.to_thread(self.save_sync)

    def metadata_dict(self) -> dict:
        a = self.agent
        return {
            "session_id": a.session_id,
            "title": (Path(a.docx_path).stem if a.docx_path else "新会话"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "docx_path": a.docx_path,
            "provider": a.llm.get_provider() if hasattr(a.llm, "get_provider") else "",
            "model": a.llm.get_model_name() if hasattr(a.llm, "get_model_name") else "",
            "workflow_state": a.workflow_state,
            "session_complete": False,
            # v2: resume 时供 server.py 推 isWaitingApproval
            "pending_approval": a._pending_approval,
            # v3: 工作模式 (fill / detail_edit)
            "mode": a.mode,
        }

    def messages_dict(self) -> dict:
        a = self.agent
        return {
            "session_id": a.session_id,
            "system_prompt": a.msg_mgr._system_prompt,
            "entries": list(a.msg_mgr._entries),
            "total_input_tokens": a.msg_mgr._total_input_tokens,
            "last_prompt_tokens": a.msg_mgr._last_prompt_tokens,
        }

    def workflow_dict(self) -> dict:
        a = self.agent
        return {
            "session_id": a.session_id,
            "workflow_state": a.workflow_state,
            "stage_called_tools": {k: sorted(v) for k, v in a.stage_called_tools.items()},
            "draft_files_written": list(a.draft_files_written),
            "round_index": a._round_index,
            # v3: 细致编辑模式状态
            "selected_tools": sorted(a.selected_tools),
            "doc_structure_cache": a._doc_structure_cache,
        }
