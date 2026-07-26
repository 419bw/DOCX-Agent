"""
DOCX Agent 核心模块：异步生成器风格的 agent 循环。
server.py 通过 async for event in agent.step() 驱动，事件实时逐个发送。
"""

import sys
from pathlib import Path

# Ensure src directory is in path for imports
sys.path.append(str(Path(__file__).parent))

import asyncio
import json
import uuid
from typing import Optional


# === v2: 避坑 1 核心 — SESSION_TOOLS 集合 ===
# 这些工具的 Python 函数签名接受 session_id, 但 LLM 看到的 tools_schema **不**含 session_id
# agent.py tool dispatcher 反射调用前会**隐式注入** session_id (= self.session_id)
# LLM 传什么都无效, 即使 LLM 幻觉瞎传 session_id 也会被 self.session_id 覆盖
# 注意: 只列 LLM **可见**工具 (在 TOOLS_SCHEMA 里), 内部 helper (如 apply_markdown_ir_after_paragraph)
# 不在此集合 — 它们通过 markdown_to_word 间接传 session_id, 不走 dispatcher
SESSION_TOOLS = {
    "write_markdown_draft",
    "read_markdown_draft",
    "parse_markdown_draft",
    "apply_markdown_ir_to_table_cell",
    "markdown_to_word",
    "analyze_docx_style_samples",
    "bind_styles_to_roles",
    "define_style_profile",
    # v2: basic_tools 沙箱化 (Phase 3a)
    "ls",
    "read",
    "analyze_image_content",
    "generate_image",  # sensenova-u1-fast 生图, 内部 sub-agent 审核-重画迭代
    "render_diagram",  # Graphviz/Mermaid via kroki.io, 用于结构化图表
    # v2: docx_tools 读类沙箱化 (Phase 3c)
    "read_docx_structure",
    "find_text",
    "diff_docx",
    # v2: docx_tools 写入类沙箱化 (Phase 3d) — 19 个
    "insert_text_at",
    "insert_text_in_table_cell",
    "insert_table_row_after",
    "set_paragraph_indent",
    "insert_table_after_paragraph",
    "insert_table_in_cell",
    "insert_table_column_after",
    "merge_table_cells_horizontal",
    "clear_table_cell",
    "delete_table_row",
    "replace_table_cell_text",
    "replace_text",
    "delete_text",
    "insert_paragraph_after",
    "set_text_format",
    "replace_text_like_sample",
    "insert_paragraph_after_like_sample",
    "replace_table_cell_like_sample",
    "insert_image_after_paragraph",
    # v2: unzip_docx 沙箱化 (Phase 3e)
    "unzip_docx",
}

from openai import APITimeoutError, APIConnectionError, BadRequestError

from llm_adapter import LLMClientAdapter
from llm_adapter.response_parser import extract_reasoning
from llm_adapter.quirks import apply_quirk, QuirkAction
from docx_tools import call_tool
from docx_tools.docx_preview_diff import build_paragraph_diff, extract_preview_event
from context_manager import MessageManager
from state_machine import WorkflowTransitions, TransitionDirective
from session_persistence import SessionPersistence
from workspace.guard import WorkspacePathError, resolve_workspace_path


# 流式调用可重试的异常（pre-stream 阶段）
_RETRYABLE_EXC = (APITimeoutError, APIConnectionError)
_MAX_RETRIES = 2
_RETRY_DELAYS = [1.0, 2.0]  # 两次重试的退避秒数


def create_log_file(session_dir: Optional[Path] = None) -> Path:
    """会话专属日志: 写到 session_dir/logs/ (如果提供), 否则全局 logs/ (向后兼容)"""
    from datetime import datetime
    if session_dir:
        log_dir = session_dir / "logs"
    else:
        log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"agent_{timestamp}.log"


def append_log(log_path, title, data=None):
    if log_path:
        from datetime import datetime
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 80}\n")
            f.write(f"{datetime.now().isoformat(timespec='seconds')} | {title}\n")
            f.write(f"{'=' * 80}\n")
            if data is None:
                return
            if isinstance(data, str):
                f.write(data)
            else:
                f.write(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            f.write("\n")

# === 提示词与状态名常量已迁移到 src/prompts.py (Step A 重构) ===
# 这里重导出以保持 `from agent import SYSTEM_PROMPT` 等旧 import 兼容
from prompts import (
    STYLE_REVIEW,
    MD_DRAFT,
    WORD_EDITING,
    DETAIL_EDIT,
    DETAIL_EDIT_BASE_TOOLS,
    REVIEW_TOOL_NAMES,
    MD_DRAFT_TOOL_NAMES,
    WORD_EDITING_TOOL_NAMES,
    SYSTEM_PROMPT,
    tool_schemas_for_state,
    tool_schemas_for_detail_edit,
    state_prompt,
    detail_edit_prompt,
)
from agents.tool_selector import select_tools


class Agent:
    """
    Agent 核心类：异步生成器风格的 agent 循环。

    使用方式：
        async for event in agent.step():
            await websocket.send_json(event)
            if event["type"] == "wait_approval":
                agent.on_user_feedback(await websocket.receive_json())

    step() 是 async generator，每次 yield 一个事件，协程自动暂停/恢复。
    """

    def __init__(self, system_prompt: str, llm_adapter: LLMClientAdapter,
                 msg_mgr: MessageManager, docx_path: str = "", log_path: Optional[Path] = None,
                 session_id: str = "", session_dir: Optional[Path] = None,
                 stream_mode: bool = True, mode: str = "fill"):
        self.system_prompt = system_prompt
        self.msg_mgr = msg_mgr
        self.llm = llm_adapter
        self.docx_path = docx_path
        self.log_path = log_path
        self.workflow_state = STYLE_REVIEW
        self.stage_called_tools = {}  # {stage_name: set(tool_names)}
        self._pending_feedback = None
        self._pending_approval = False  # v2: resume 时由 server 读出, 判断前端要不要展示"待审批"按钮
        self._round_index = 0
        self.draft_files_written: list[str] = []   # 追踪 MD_DRAFT 阶段写过的所有 .md 路径
        # === B 方案: finish_reason=None 流不完整重试计数 ===
        # 商汤 SenseNova 偶发"流静默关闭"(OpenAI 协议异常): finish_reason 缺失, content/tool_calls 都空
        # 表现为 reasoning-only 死循环 (复现: session-20260609-205746 第 13/14 轮)
        # 策略: 检测到 finish_reason=None + 无 tool_calls → 不污染 conversation, 重发请求
        self._stream_incomplete_retries = 0
        # === 流式 / 非流式 开关 ===
        # 用途:商汤 SenseNova 等 SSE 协议层有 bug 的 provider 在流式 reasoning→content 切换时
        # 偶发流静默关闭(2026-06-10 session-20260610-100416 04 章节 stall),前端点 toggle
        # 切到非流式,下一轮 step() 走 create_chat_completion_blocking()。
        # 默认 True(流式)保持现有 live reasoning UX;切换在下一轮请求生效,不打断当前请求。
        # stream_mode 由 server.py 在构造 Agent 时传入(读自 WS start 帧) — 让用户能
        # 在新会话启动前先把 toggle 拨到非流式,新会话直接以非流式开始(避免触发 stall 后再切)。
        self._stream_mode: bool = stream_mode
        # === v3: 细致编辑模式 ===
        # mode: "fill" (三阶段填充, 默认) | "detail_edit" (自由编辑 + 工具选用)
        self.mode: str = mode
        self.selected_tools: set[str] = set()  # detail_edit 模式的动态工具集
        self._doc_structure_cache: str = ""  # 首次 read_docx_structure 结果缓存
        # === v2: 持久化相关 ===
        self.session_id = session_id
        self.session_dir = session_dir  # Path("out") / "sessions" / session_id
        # Step B: 持久化层已抽出到 src/session_persistence.py
        # 用 weakref 避免双向循环引用 (用户补丁 1), lock 移入 persistence 内部
        self._persistence = SessionPersistence(self)
        # v3: 策略模式 — 根据 mode 创建对应策略
        from modes import get_strategy
        self._mode = get_strategy(mode)

    # ─── v2 持久化: 委托给 SessionPersistence ────

    def _checkpoint(self) -> None:
        """5 个 Checkpoint 触发点统一调用: 委托给 SessionPersistence"""
        self._persistence.checkpoint()

    def save_to_disk(self) -> None:
        """同步写盘 — 委托给 SessionPersistence (向后兼容, 测试 / 旧调用方用)"""
        self._persistence.save_sync()

    @classmethod
    def load_from_disk(
        cls, session_dir: Path, llm_adapter: LLMClientAdapter,
        system_prompt: str, docx_path: str = "", log_path: Optional[Path] = None
    ) -> "Agent":
        """从 session_dir 反序列化 Agent 状态 (Step 2 server.py resume 时调用)"""
        # Step B: 读 3 个 JSON 委托给 SessionPersistence
        metadata, messages_data, workflow = SessionPersistence.read_session_files(session_dir)

        msg_mgr = MessageManager(system_prompt)
        msg_mgr._entries = list(messages_data["entries"])
        # v3: 加载时清理旧数据 — 修复 DeepSeek 400 反复出现
        # 旧版本 (c2d4322 之前) 保存的 messages.json 含 {"role": "assistant", "tool_calls": []}
        # DeepSeek 严格校验会报 400 (Messages with role 'tool' must be a response to ...)
        msg_mgr._sanitize_entries()
        msg_mgr._total_input_tokens = messages_data["total_input_tokens"]
        msg_mgr._last_prompt_tokens = messages_data["last_prompt_tokens"]

        agent = cls(
            system_prompt=system_prompt, llm_adapter=llm_adapter,
            msg_mgr=msg_mgr, docx_path=docx_path, log_path=log_path,
            session_id=metadata["session_id"], session_dir=session_dir,
            mode=metadata.get("mode", "fill"),  # v3: 恢复模式
        )
        agent.workflow_state = workflow["workflow_state"]
        agent.stage_called_tools = {k: set(v) for k, v in workflow["stage_called_tools"].items()}
        agent.draft_files_written = list(workflow["draft_files_written"])
        agent._round_index = workflow["round_index"]
        agent._pending_approval = metadata.get("pending_approval", False)  # v2: 恢复"是否在等审批"标志
        # v3: 恢复细致编辑模式状态
        agent.selected_tools = set(workflow.get("selected_tools", []))
        agent._doc_structure_cache = workflow.get("doc_structure_cache", "")
        return agent

    # ─── 日志 ────────────────────────────────────────────

    def _append_log(self, title: str, data=None):
        if self.log_path:
            from datetime import datetime
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 80}\n")
                f.write(f"{datetime.now().isoformat(timespec='seconds')} | {title}\n")
                f.write(f"{'=' * 80}\n")
                if data is None:
                    return
                if isinstance(data, str):
                    f.write(data)
                else:
                    f.write(json.dumps(data, ensure_ascii=False, indent=2, default=str))
                f.write("\n")

    # ─── 核心驱动：异步生成器 ────────────────────────────

    def on_user_feedback(self, client_msg: dict):
        """WS handler 在收到用户操作后调用, v3 路由扩展.

        支持的 type:
          - "continue": 用户在 paused 状态 (如 word_editing 中断) 发了新消息
            → append_user 注入到 messages, 下次 step() 会带去 LLM
          - "approve": 用户在 wait_approval 状态点审批按钮
            → 存入 _pending_feedback, 走原 step() 内部 approved 逻辑
          - 其他 (兼容老版本): 透传到 _pending_feedback, 由 step() 内部判断
        """
        msg_type = client_msg.get("type")
        if msg_type == "continue":
            # 用户在 paused 状态发了新 prompt → 当作新一轮对话入口
            prompt = (client_msg.get("prompt") or "").strip()
            if prompt:
                self.msg_mgr.append_user(prompt)
            # 注意: 不设 _pending_feedback 也不改 _pending_approval
            # 下次 step() 会把这条 user 消息作为上下文带去 LLM
        elif msg_type == "approve":
            # 审批: 透传到 _pending_feedback, 走原 step() 内部判断
            self._pending_feedback = client_msg
            # v2: 不再是"等待审批"状态 (step() 恢复后会判断 approved 分支)
            self._pending_approval = False
        else:
            # 兜底: 其他潜在逻辑 (如单独的 "feedback" type) — 走原逻辑
            self._pending_feedback = client_msg
            self._pending_approval = False

    def set_stream_mode(self, stream_mode: bool) -> None:
        """切换流式 / 非流式 — 前端 toggle 调用, 下一轮 step() 生效。

        行为:
        - stream_mode=True:  下一轮走 create_chat_completion(stream=True) — 默认
        - stream_mode=False: 下一轮走 create_chat_completion_blocking() — 用于 SenseNova
                              等 SSE 协议层有 bug 的 provider

        注意:
        - 切换不打断当前正在进行的请求, 下一轮请求才用新模式 (原话 "下一条消息开始用新模式")
        - 不持久化: 重启 / 刷新页面后回到默认 (按用户原话不落盘)
        - server.py 在收到 WS "set_stream_mode" 消息时调用本方法
        """
        self._stream_mode = bool(stream_mode)
        self._append_log("stream_mode_changed", {"stream_mode": self._stream_mode})

    # ─── v3: DOCX 实时预览钩子 (markdown_to_word 完成 → 推 docx_preview_ready) ───

    async def _maybe_emit_docx_preview(self, tool_name: str, tool_result_str: str) -> dict | None:
        """[v3] markdown_to_word 完成后, 算 diff 并组装 docx_preview_ready 事件.

        触发条件:
          - tool_name == "markdown_to_word" (其他工具直接返回 None)

        失败场景 (全部静默返回 None, caller 不 yield, 主流程不感知):
          - JSON 解析失败 / status != "ok" / 缺关键字段
          - 路径越界 / 文件不存在 (resolve_workspace_path 失败)
          - zip 损坏 / 解析异常 (build_paragraph_diff 静默吞)
        """
        if tool_name != "markdown_to_word":
            return None

        # 1. 解析 markdown_to_word 的 result JSON
        preview_data = extract_preview_event(tool_result_str)
        if not preview_data:
            return None

        # 2. 沙箱校验两个路径 (LLM 可能幻觉路径, 必须 resolve_workspace_path)
        try:
            input_path = resolve_workspace_path(
                self.session_id, preview_data["docx_path"],
                must_exist=True, must_be_file=True,
            )
            output_path = resolve_workspace_path(
                self.session_id, preview_data["output_path"],
                must_exist=True, must_be_file=True,
            )
        except WorkspacePathError as exc:
            self._append_log("docx_preview_skip", {
                "reason": "resolve_workspace_path failed",
                "error": str(exc),
            })
            return None
        except Exception as exc:
            self._append_log("docx_preview_skip", {
                "reason": "unexpected resolve error",
                "error": repr(exc),
            })
            return None

        # 3. 算 diff — 走线程池避免阻塞事件循环 (读两个 docx + 算 hash)
        try:
            diff = await asyncio.to_thread(build_paragraph_diff, input_path, output_path)
        except Exception as exc:
            self._append_log("docx_preview_skip", {
                "reason": "build_paragraph_diff failed",
                "error": repr(exc),
            })
            return None

        # 4. 组装事件 payload
        try:
            docx_mtime_ms = int(output_path.stat().st_mtime * 1000)
        except Exception:
            docx_mtime_ms = 0

        return {
            "type": "docx_preview_ready",
            "preview_path": preview_data["output_path"],
            "input_path": preview_data["docx_path"],
            "docx_mtime_ms": docx_mtime_ms,
            "paragraph_changes": diff.get("paragraph_changes", []),
            "changed_files": diff.get("changed_files", []),
            "diagnostics": preview_data.get("diagnostics", []),
            "action_count": preview_data.get("action_count", 0),
            "support_summary": preview_data.get(
                "support_summary", {"native": 0, "degraded": 0, "rejected": 0}
            ),
        }

    # ─── 共享基础设施: LLM 调用 + 解析 ──────────────────

    async def call_llm_round(self, request_messages, current_tool_schemas, result: dict):
        """共享: LLM 调用 + 重试 + 流式/非流式解析 + quirk 处理。

        async generator, yield 事件 (reasoning/content/reasoning_end/retrying/error)。
        解析结果填入 result dict:
          tool_calls_map, accumulated_content, accumulated_reasoning,
          finish_reason, usage, quirk_retry, error
        """
        result.update({"tool_calls_map": {}, "accumulated_content": "", "accumulated_reasoning": "",
                        "finish_reason": None, "usage": None, "quirk_retry": False, "error": False})

        # --- Pre-stream retry ---
        response_or_stream = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                if self._stream_mode:
                    response_or_stream = self.llm.create_chat_completion(
                        messages=request_messages, tools=current_tool_schemas, stream=True)
                else:
                    response_or_stream = self.llm.create_chat_completion_blocking(
                        messages=request_messages, tools=current_tool_schemas)
                break
            except _RETRYABLE_EXC as e:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    mode = "stream" if self._stream_mode else "blocking"
                    self._append_log("流式重试", {"attempt": attempt + 1, "error": str(e), "delay": delay, "mode": mode})
                    yield {"type": "retrying", "attempt": attempt + 1, "delay": delay, "error": str(e)}
                    await asyncio.sleep(delay)
                    continue
                mode = "stream" if self._stream_mode else "blocking"
                self._append_log("流式重试耗尽", {"error": str(e), "total_attempts": attempt + 1, "mode": mode})
                yield {"type": "error", "message": f"调用大模型失败（重试 {attempt + 1} 次后）: {e}"}
                result["error"] = True
                return
            except Exception as e:
                mode = "stream" if self._stream_mode else "blocking"
                self._append_log("模型调用失败", {"error": str(e), "mode": mode})
                yield {"type": "error", "message": f"调用大模型失败: {str(e)}"}
                result["error"] = True
                return

        # --- 拆解响应 ---
        tool_calls_map: dict = {}
        accumulated_content = ""
        accumulated_reasoning = ""
        reasoning_yielded: bool = False
        finish_reason = None
        usage = None
        stream_error: Exception | None = None

        if self._stream_mode:
            try:
                for chunk in response_or_stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage:
                        usage = chunk_usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    rc = extract_reasoning(delta, self.llm.reasoning_field)
                    if rc:
                        accumulated_reasoning += rc
                        reasoning_yielded = True
                        self._append_log("chunk_event", {"round": self._round_index, "type": "reasoning", "len": len(rc), "cum": len(accumulated_reasoning)})
                        yield {"type": "reasoning", "delta": rc}
                    c = getattr(delta, "content", None)
                    if c:
                        accumulated_content += c
                        self._append_log("chunk_event", {"round": self._round_index, "type": "content", "len": len(c), "cum": len(accumulated_content)})
                        yield {"type": "content", "delta": c}
                    tcs = getattr(delta, "tool_calls", None)
                    if tcs:
                        for tc in tcs:
                            idx = tc.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": tc.id or "",
                                    "name": (tc.function.name if tc.function else "") or "",
                                    "arguments": (tc.function.arguments if tc.function else "") or "",
                                }
                            else:
                                if tc.function:
                                    if tc.function.name:
                                        tool_calls_map[idx]["name"] += tc.function.name
                                    if tc.function.arguments:
                                        tool_calls_map[idx]["arguments"] += tc.function.arguments
                                if tc.id:
                                    tool_calls_map[idx]["id"] = tc.id
            except Exception as e:
                stream_error = e
        else:
            try:
                response = response_or_stream
                usage = getattr(response, "usage", None)
                if response.choices:
                    choice = response.choices[0]
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
                    msg = getattr(choice, "message", None)
                    if msg is not None:
                        rc = getattr(msg, "reasoning_content", None)
                        if not rc:
                            extra = getattr(msg, "model_extra", None)
                            if isinstance(extra, dict):
                                rc = extra.get("reasoning")
                        if rc:
                            accumulated_reasoning += rc
                            reasoning_yielded = True
                            self._append_log("chunk_event", {"round": self._round_index, "type": "reasoning", "len": len(rc), "cum": len(accumulated_reasoning), "mode": "blocking"})
                            yield {"type": "reasoning", "delta": rc}
                        c = getattr(msg, "content", None)
                        if c:
                            accumulated_content += c
                            self._append_log("chunk_event", {"round": self._round_index, "type": "content", "len": len(c), "cum": len(accumulated_content), "mode": "blocking"})
                            yield {"type": "content", "delta": c}
                        tcs = getattr(msg, "tool_calls", None)
                        if tcs:
                            for tc in tcs:
                                idx = getattr(tc, "index", 0) or 0
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        "id": tc.id or "",
                                        "name": (tc.function.name if tc.function else "") or "",
                                        "arguments": (tc.function.arguments if tc.function else "") or "",
                                    }
            except Exception as e:
                stream_error = e

        if reasoning_yielded:
            yield {"type": "reasoning_end"}

        # --- Mid-stream failure ---
        if stream_error is not None:
            self._append_log("模型流中断", {"error": str(stream_error), "partial_content_len": len(accumulated_content), "partial_reasoning_len": len(accumulated_reasoning)})
            yield {"type": "error", "message": f"流式调用中断: {stream_error}"}
            result["error"] = True
            return

        # --- Log ---
        log_msg = {"role": "assistant", "finish_reason": finish_reason}
        if tool_calls_map:
            log_msg["tool_calls"] = [{"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["arguments"]}} for v in tool_calls_map.values()]
        if accumulated_content:
            log_msg["content"] = accumulated_content
        if accumulated_reasoning:
            log_msg["reasoning_content"] = accumulated_reasoning
        self._append_log(f"第 {self._round_index} 轮模型响应", log_msg)

        # --- Token tracking ---
        self.msg_mgr.update_token_count(usage)
        if usage:
            self._append_log(f"第 {self._round_index} 轮 token", {"prompt_tokens": getattr(usage, "prompt_tokens", 0), "completion_tokens": getattr(usage, "completion_tokens", 0)})

        # --- Quirk 处理 ---
        _MAX_INCOMPLETE_STREAM_RETRY = 2
        quirk_retry_triggered = False
        for quirk_name in self.llm.quirks:
            directive = apply_quirk(quirk_name, {
                "finish_reason": finish_reason,
                "tool_calls_map": tool_calls_map,
                "accumulated_content": accumulated_content,
                "accumulated_reasoning": accumulated_reasoning,
            })
            if directive.action == QuirkAction.RETRY_REQUEST:
                if self._stream_incomplete_retries < _MAX_INCOMPLETE_STREAM_RETRY:
                    self._stream_incomplete_retries += 1
                    self._append_log("stream_incomplete_retry", {"round": self._round_index, "attempt": self._stream_incomplete_retries, "trigger_quirk": quirk_name, "reason": directive.reason, "had_content": len(accumulated_content) > 0, "had_reasoning": len(accumulated_reasoning) > 0, "reasoning_len": len(accumulated_reasoning)})
                    quirk_retry_triggered = True
                    break
                self._append_log("stream_incomplete_retry_exhausted", {"round": self._round_index, "max_retries": _MAX_INCOMPLETE_STREAM_RETRY, "trigger_quirk": quirk_name})
                self._stream_incomplete_retries = 0
                break

        if quirk_retry_triggered:
            result["quirk_retry"] = True
            result.update({"tool_calls_map": tool_calls_map, "accumulated_content": accumulated_content, "accumulated_reasoning": accumulated_reasoning, "finish_reason": finish_reason, "usage": usage})
            return

        self._stream_incomplete_retries = 0
        result.update({"tool_calls_map": tool_calls_map, "accumulated_content": accumulated_content, "accumulated_reasoning": accumulated_reasoning, "finish_reason": finish_reason, "usage": usage})

    # ─── 共享基础设施: 单工具 dispatch ──────────────────

    def dispatch_single_tool(self, name: str, args: str, current_tool_names: set, effective_state: str) -> str:
        """共享: session_id 注入 + call_tool 反射调用。返回 result JSON 字符串。"""
        if name not in current_tool_names:
            return json.dumps({"status": "error", "tool": name, "message": f"当前状态 ({effective_state}) 不允许调用该工具"}, ensure_ascii=False)

        call_args_str = args
        if name in SESSION_TOOLS:
            try:
                call_args_dict = json.loads(args) if isinstance(args, str) else dict(args)
            except (json.JSONDecodeError, TypeError):
                call_args_dict = {}
            call_args_dict["session_id"] = self.session_id
            call_args_str = json.dumps(call_args_dict, ensure_ascii=False)
        try:
            import asyncio as _aio
            # 注意: 此方法在 async 上下文中被调用, 但 call_tool 是同步的
            # 策略层负责 await asyncio.to_thread 包装
            return call_tool(name, call_args_str)
        except Exception as e:
            return json.dumps({"status": "error", "tool": name, "message": f"工具执行异常: {str(e)}"}, ensure_ascii=False)

    # ─── 核心驱动: 薄编排器 ────────────────────────────

    async def step(self):
        """异步生成器: 委托给当前模式策略。

        resume 时 yield paused 不调 LLM; done 状态直接 yield done。
        正常流程委托给 self._mode.run(self)。
        """
        if getattr(self, "_is_resume", False):
            self._is_resume = False
            yield {
                "type": "paused",
                "phase": self.workflow_state,
                "is_waiting_approval": self._pending_approval,
                "reason": "resume_paused",
            }
            return

        if self.workflow_state == "done":
            yield {"type": "done", "content": ""}
            return

        self._should_return = False  # 策略层设置, 供 fill 模式状态机用
        async for event in self._mode.run(self):
            if event.get("_break"):
                continue  # 内部信号, 不转发给前端
            yield event