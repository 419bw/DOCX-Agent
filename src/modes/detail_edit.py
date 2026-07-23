"""DetailEditMode — 细致编辑模式策略。

工具选用 agent 选出工具子集 → 自由编辑循环 → done（无审批）。
编辑 agent 可通过 request_more_tools 请求扩充工具。
"""

import json

from prompts import (
    DETAIL_EDIT, DETAIL_EDIT_BASE_TOOLS,
    tool_schemas_for_detail_edit, detail_edit_prompt,
)
from docx_tools import call_tool
from docx_tools.docx_preview_diff import build_paragraph_diff
from workspace.guard import resolve_workspace_path
from agents.tool_selector import select_tools
from .base import BaseMode


class DetailEditMode(BaseMode):

    async def run(self, agent):
        # === 首次进入: 工具选用 ===
        if not agent.selected_tools:
            async for event in self._init_tools(agent):
                yield event

        # === 自由编辑循环 ===
        while True:
            agent._round_index += 1
            current_tool_schemas = tool_schemas_for_detail_edit(agent.selected_tools)
            state_prompt_text = detail_edit_prompt(current_tool_schemas)
            current_tool_names = {s["function"]["name"] for s in current_tool_schemas}

            request_messages = agent.msg_mgr.build_request_messages(state_prompt_text)

            yield {
                "type": "round_start",
                "round": agent._round_index,
                "workflow_state": DETAIL_EDIT,
                "allowed_tools": list(current_tool_names),
                "token_count": agent.msg_mgr.last_prompt_tokens,
            }
            agent._checkpoint()

            agent._append_log(f"第 {agent._round_index} 轮模型请求", {
                "workflow_state": DETAIL_EDIT,
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
                async for event in self._dispatch_tools(agent, tool_calls_map, accumulated_content, accumulated_reasoning, current_tool_names):
                    yield event
                continue

            # --- 无工具调用: 处理文本响应 ---
            agent.msg_mgr.append_assistant([], accumulated_content, accumulated_reasoning)

            content_stripped = (accumulated_content or "").strip()
            if len(content_stripped) < 5:
                guidance = "请基于当前可用工具直接执行编辑操作，或输出变更摘要。"
                agent._append_log("空响应自动引导", {"workflow_state": DETAIL_EDIT, "content_length": len(content_stripped)})
                agent.msg_mgr.append_user(guidance)
                yield {"type": "content", "delta": f"\n\n*[系统引导] {guidance}*"}
                continue

            # --- 细致编辑完成 (无审批, 直接 done) ---
            agent._append_log("细致编辑流完成", {"state": DETAIL_EDIT})
            # 注意: 不设 workflow_state = "done", 保持 DETAIL_EDIT
            # 这样 resume 时 step() 不会直接 return, 用户可以继续发消息编辑
            agent._checkpoint()
            yield {"type": "done", "content": accumulated_content}
            return

    # ─── 工具选用初始化 ────────────────────────────────────

    async def _init_tools(self, agent):
        import asyncio

        yield {"type": "tool_selection_start"}
        agent._append_log("detail_edit 工具选用开始", {"docx_path": agent.docx_path})

        # 1. 程序化调 read_docx_structure 拿文档结构
        doc_structure = "{}"
        if agent.docx_path:
            try:
                rs_args = json.dumps({
                    "session_id": agent.session_id,
                    "docx_path": agent.docx_path,
                }, ensure_ascii=False)
                doc_structure = await asyncio.to_thread(call_tool, "read_docx_structure", rs_args)
            except Exception as e:
                agent._append_log("read_docx_structure 失败", {"error": str(e)})
                doc_structure = json.dumps({"error": str(e)}, ensure_ascii=False)
        agent._doc_structure_cache = doc_structure

        # 2. 提取用户原始请求
        user_prompt = ""
        for entry in agent.msg_mgr._entries:
            if entry.get("role") == "user":
                user_prompt = entry.get("content", "")
                break

        # 3. 工具选用 LLM 调用
        try:
            selected = await asyncio.to_thread(
                select_tools, agent.llm, user_prompt, doc_structure,
            )
            agent.selected_tools = set(selected)
        except Exception as e:
            agent._append_log("工具选用失败, 降级为基础工具集", {"error": str(e)})
            agent.selected_tools = set(DETAIL_EDIT_BASE_TOOLS)

        agent._append_log("detail_edit 工具选用完成", {"selected_tools": sorted(agent.selected_tools)})
        yield {"type": "tool_selection_end", "selected_tools": sorted(agent.selected_tools)}
        agent._checkpoint()

    # ─── 工具 dispatch ────────────────────────────────────

    async def _dispatch_tools(self, agent, tool_calls_map, accumulated_content, accumulated_reasoning, current_tool_names):
        import asyncio
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

            # request_more_tools: 内部处理, 不走 call_tool
            if name == "request_more_tools":
                result = await self._handle_request_more_tools(agent, args)
            else:
                # 写前快照 (供段落级 diff 高亮)
                snapshot_path = None
                try:
                    _sa = json.loads(args) if isinstance(args, str) else args
                    _dp = _sa.get("docx_path", "")
                    if _dp:
                        _orig = resolve_workspace_path(agent.session_id, _dp, must_exist=True, must_be_file=True)
                        snapshot_path = _orig.with_suffix(".docx.snapshot")
                        import shutil
                        shutil.copy2(_orig, snapshot_path)
                except Exception:
                    snapshot_path = None

                result = await asyncio.to_thread(agent.dispatch_single_tool, name, args, current_tool_names, DETAIL_EDIT)

            agent._append_log(f"工具结果: {name}", result)
            yield {"type": "tool_end", "name": name, "result": result}
            agent._checkpoint()
            agent.msg_mgr.append_tool_result(tc["id"], result)

            # request_more_tools → 推 tool_selection_end 更新前端工具集
            if name == "request_more_tools":
                try:
                    _rmt = json.loads(result)
                    yield {"type": "tool_selection_end", "selected_tools": _rmt.get("total_tools", [])}
                except Exception:
                    pass

            # 写入工具成功后推预览 (含段落级 diff 高亮)
            if name != "request_more_tools":
                async for event in self._emit_preview(agent, name, result, snapshot_path):
                    yield event

    # ─── 预览 (含 diff) ──────────────────────────────────

    async def _emit_preview(self, agent, name, result, snapshot_path):
        import asyncio

        try:
            _r = json.loads(result)
            if _r.get("status") == "ok" and _r.get("output_path"):
                _out = resolve_workspace_path(
                    agent.session_id, _r["output_path"],
                    must_exist=True, must_be_file=True,
                )
                _changes = []
                _changed_files = []
                if snapshot_path and snapshot_path.exists():
                    _diff = await asyncio.to_thread(build_paragraph_diff, snapshot_path, _out)
                    _changes = _diff.get("paragraph_changes", [])
                    _changed_files = _diff.get("changed_files", [])
                yield {
                    "type": "docx_preview_ready",
                    "preview_path": _r["output_path"],
                    "input_path": _r.get("docx_path", ""),
                    "docx_mtime_ms": int(_out.stat().st_mtime * 1000),
                    "paragraph_changes": _changes,
                    "changed_files": _changed_files,
                    "diagnostics": [],
                    "action_count": 1,
                    "support_summary": {"native": 1, "degraded": 0, "rejected": 0},
                }
        except Exception:
            pass
        finally:
            try:
                if snapshot_path and snapshot_path.exists():
                    snapshot_path.unlink()
            except Exception:
                pass

    # ─── request_more_tools ───────────────────────────────

    async def _handle_request_more_tools(self, agent, args_str):
        import asyncio

        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
        except (json.JSONDecodeError, TypeError):
            args = {}
        reason = args.get("reason", "未说明原因")

        agent._append_log("request_more_tools", {"reason": reason})

        user_prompt = ""
        for entry in agent.msg_mgr._entries:
            if entry.get("role") == "user":
                user_prompt = entry.get("content", "")
                break

        try:
            new_selected = await asyncio.to_thread(
                select_tools,
                agent.llm,
                user_prompt,
                agent._doc_structure_cache or "{}",
                current_tools=agent.selected_tools,
                reason=reason,
            )
            old_tools = set(agent.selected_tools)
            agent.selected_tools = set(new_selected)
            added = sorted(agent.selected_tools - old_tools)
        except Exception as e:
            agent._append_log("工具扩充失败", {"error": str(e)})
            added = []

        agent._append_log("工具扩充完成", {
            "reason": reason,
            "added": added,
            "total": sorted(agent.selected_tools),
        })
        agent._checkpoint()

        return json.dumps({
            "status": "ok",
            "message": f"工具集已更新。新增工具: {added if added else '无（当前工具集已足够）'}",
            "added_tools": added,
            "total_tools": sorted(agent.selected_tools),
        }, ensure_ascii=False)
