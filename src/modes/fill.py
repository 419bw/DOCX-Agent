"""FillMode — 三阶段填充模式策略。

样式审核 (Style Review) → Markdown 草稿 (MD Draft) → Word 写入编译 (Word Editing)
每阶段有审批挂起 (wait_approval)，LLM 只能用当前阶段允许的工具集。
"""

import asyncio
from pathlib import Path

from prompts import (
    STYLE_REVIEW, MD_DRAFT, WORD_EDITING,
    tool_schemas_for_state, state_prompt,
)
from state_machine import WorkflowTransitions
from .base import BaseMode


class FillMode(BaseMode):

    async def run(self, agent):
        while True:
            agent._round_index += 1
            current_tool_schemas = tool_schemas_for_state(agent.workflow_state)
            state_prompt_text = state_prompt(agent.workflow_state, current_tool_schemas)
            current_tool_names = {s["function"]["name"] for s in current_tool_schemas}

            request_messages = agent.msg_mgr.build_request_messages(state_prompt_text)

            yield {
                "type": "round_start",
                "round": agent._round_index,
                "workflow_state": agent.workflow_state,
                "allowed_tools": list(current_tool_names),
                "token_count": agent.msg_mgr.last_prompt_tokens,
            }
            agent._checkpoint()

            agent._append_log(f"第 {agent._round_index} 轮模型请求", {
                "workflow_state": agent.workflow_state,
                "message_count": len(request_messages),
                "tool_names": sorted(list(current_tool_names)),
            })

            # --- LLM 调用 + 解析 (共享基础设施) ---
            result = {}
            async for event in agent.call_llm_round(request_messages, current_tool_schemas, result):
                yield event

            if result.get("error"):
                return
            if result.get("quirk_retry"):
                continue

            tool_calls_map = result["tool_calls_map"]
            accumulated_content = result["accumulated_content"]
            accumulated_reasoning = result["accumulated_reasoning"]

            # --- 有工具调用 ---
            if tool_calls_map:
                async for event in self._dispatch_tools(agent, tool_calls_map, accumulated_content, accumulated_reasoning, current_tool_names, agent.workflow_state):
                    yield event
                continue

            # --- 无工具调用: 处理文本响应 ---
            agent.msg_mgr.append_assistant([], accumulated_content, accumulated_reasoning)

            content_stripped = (accumulated_content or "").strip()
            if len(content_stripped) < 5:
                if agent.workflow_state == STYLE_REVIEW:
                    guidance = "你当前处于样式审核阶段，请基于已读取的文档信息直接输出样式分析结果（列出 sample_id 与对应格式特征），不要尝试查看其他目录或文件。"
                elif agent.workflow_state == MD_DRAFT:
                    guidance = "请直接输出 Markdown 草稿内容或给出下一步草稿计划。"
                else:
                    guidance = "请基于当前可用工具直接执行操作或给出分析结果。"
                agent._append_log("空响应自动引导", {"workflow_state": agent.workflow_state, "content_length": len(content_stripped)})
                agent.msg_mgr.append_user(guidance)
                yield {"type": "content", "delta": f"\n\n*[系统引导] {guidance}*"}
                continue

            # --- 状态机转换 ---
            if agent.workflow_state == STYLE_REVIEW:
                async for event in self._evaluate_style_review(agent, accumulated_content):
                    yield event
                    if event.get("_break"):
                        break
                if agent._should_return:
                    return
                continue

            if agent.workflow_state == MD_DRAFT:
                async for event in self._evaluate_md_draft(agent, accumulated_content):
                    yield event
                    if event.get("_break"):
                        break
                if agent._should_return:
                    return
                continue

            # WORD_EDITING 结束
            agent._append_log("写入与编译流完成", {"state": agent.workflow_state})
            agent._checkpoint()
            yield {"type": "done", "content": accumulated_content}
            return

    # ─── 工具 dispatch ────────────────────────────────────

    async def _dispatch_tools(self, agent, tool_calls_map, accumulated_content, accumulated_reasoning, current_tool_names, effective_state):
        import json
        import uuid

        tool_calls_list = [
            {
                "id": tool_calls_map[idx]["id"] or f"call_{idx}_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": tool_calls_map[idx]["name"],
                    "arguments": tool_calls_map[idx]["arguments"],
                },
            }
            for idx in sorted(tool_calls_map.keys())
        ]
        agent.msg_mgr.append_assistant(tool_calls_list, accumulated_content, accumulated_reasoning)
        agent._checkpoint()

        for tc in tool_calls_list:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]

            agent._append_log(f"调用工具: {name}", {"tool": name, "arguments": args})
            agent._append_log("chunk_event", {"round": agent._round_index, "type": "tool_start", "name": name})
            yield {"type": "tool_start", "name": name, "arguments": args}
            agent._checkpoint()

            result = await asyncio.to_thread(agent.dispatch_single_tool, name, args, current_tool_names, effective_state)

            agent._append_log(f"工具结果: {name}", result)
            yield {"type": "tool_end", "name": name, "result": result}
            agent._checkpoint()
            agent.msg_mgr.append_tool_result(tc["id"], result)

            # fill 模式: markdown_to_word 预览
            preview_event = await agent._maybe_emit_docx_preview(name, result)
            if preview_event:
                yield preview_event

            # 追踪 write_markdown_draft 写入的文件路径
            if name == "write_markdown_draft":
                try:
                    result_data = json.loads(result)
                    if result_data.get("status") == "ok" and result_data.get("markdown_path"):
                        agent.draft_files_written.append(result_data["markdown_path"])
                except Exception:
                    pass

            if agent.workflow_state not in agent.stage_called_tools:
                agent.stage_called_tools[agent.workflow_state] = set()
            agent.stage_called_tools[agent.workflow_state].add(name)

    # ─── STYLE_REVIEW 状态机 ──────────────────────────────

    async def _evaluate_style_review(self, agent, accumulated_content):
        agent._should_return = False
        while True:
            directive = WorkflowTransitions.evaluate_style_review(
                agent.stage_called_tools, agent._pending_feedback,
            )
            agent._pending_feedback = None
            if directive.action == "correct":
                agent._append_log("阶段校验失败", {"reason": "未完成样式分析与角色绑定", "correction": directive.user_message})
                agent.msg_mgr.append_user(directive.user_message)
                yield directive.extra_event
                yield {"_break": True}
                return
            if directive.action == "yield_approval":
                agent._append_log("等待用户确认样式审核", {"state": agent.workflow_state})
                agent._pending_approval = True
                agent._checkpoint()
                yield {"type": "wait_approval", "phase": directive.phase, "content": accumulated_content}
                continue
            if directive.action == "advance":
                agent._append_log("用户样式审核确认", {"approved": True, "feedback": ""})
                agent.stage_called_tools.pop(STYLE_REVIEW, None)
                agent.workflow_state = directive.to_state
                if directive.should_clear_drafts:
                    agent.draft_files_written = []
                agent._append_log("状态流转", {"from": STYLE_REVIEW, "to": MD_DRAFT})
                agent.msg_mgr.append_user(directive.user_message)
                yield {"_break": True}
                return
            if directive.action == "revise":
                if directive.extra_event and directive.extra_event.get("type") == "error":
                    agent._append_log("非预期指令，关闭连接", {"feedback": directive.user_message})
                    yield directive.extra_event
                    agent._should_return = True
                    return
                agent._append_log("用户样式审核确认", {"approved": False, "feedback": directive.user_message})
                agent.msg_mgr.append_user(directive.user_message)
                yield {"_break": True}
                return

    # ─── MD_DRAFT 状态机 ──────────────────────────────────

    async def _evaluate_md_draft(self, agent, accumulated_content):
        agent._should_return = False
        draft_parts: list[str] = []
        for path_str in agent.draft_files_written:
            p = Path(path_str)
            if p.exists():
                draft_parts.append(f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}")
        draft_content = "\n\n".join(draft_parts) if draft_parts else ""

        while True:
            directive = WorkflowTransitions.evaluate_md_draft(
                agent.stage_called_tools, agent.draft_files_written, draft_content, agent._pending_feedback,
            )
            agent._pending_feedback = None
            if directive.action == "yield_approval":
                agent._append_log("等待用户确认 Markdown 草稿", {"state": agent.workflow_state})
                agent._pending_approval = True
                agent._checkpoint()
                yield {"type": "wait_approval", "phase": directive.phase, "content": accumulated_content, "draft_content": draft_content}
                continue
            if directive.action == "advance":
                agent._append_log("用户草稿确认", {"approved": True, "feedback": ""})
                agent.workflow_state = directive.to_state
                agent._append_log("状态流转", {"from": MD_DRAFT, "to": WORD_EDITING})
                agent.msg_mgr.append_user(directive.user_message)
                yield {"_break": True}
                return
            if directive.action == "revise":
                if directive.extra_event and directive.extra_event.get("type") == "error":
                    agent._append_log("非预期指令，关闭连接", {"feedback": directive.user_message})
                    yield directive.extra_event
                    agent._should_return = True
                    return
                agent._append_log("用户草稿确认", {"approved": False, "feedback": directive.user_message})
                if directive.should_clear_drafts:
                    agent.draft_files_written = []
                agent.msg_mgr.append_user(directive.user_message)
                yield {"_break": True}
                return
