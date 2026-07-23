import os
import sys
import json
import shutil
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.append(str(Path(__file__).parent))

from llm_adapter import LLMClientAdapter
from docx_tools import TOOLS_SCHEMA, call_tool, render_tools_prompt
from context_manager import MessageManager
from docx_tools.analyze_docx_style_samples import analyze_docx_style_samples
from md_tools.markdown_to_word import markdown_to_word
from docx_tools.diff_docx import diff_docx
from agent import Agent, SYSTEM_PROMPT, create_log_file, append_log
from workspace.api import router as workspace_router  # v2: 上传 / 列表 / 删除 endpoints
from workspace.guard import WorkspacePathError, resolve_workspace_path  # v3: /api/word/preview 沙箱校验

app = FastAPI(title="DOCX-Agent Backend API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# v2: 挂载 workspace 路由 (上传 / 列表 / 删除 / 清空)
# prefix 决定 endpoints 形式: /api/sessions/{id}/upload 等
app.include_router(workspace_router, prefix="/api/sessions")

SESSIONS_ROOT = Path("out/sessions")  # v2: 每个 session 一个目录 (含 drafts/ / style_profiles/ / uploads/ 子目录)
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)


# === v2 Pydantic models: 删 ParseDraftRequest / SaveDraftRequest (草稿文件在 session_dir/drafts/, 不再走全局) ===
# === v2 新增: UploadRequest 接受 session_id, 文件写到 out/sessions/<session_id>/uploads/ ===


class StyleAnalyzeRequest(BaseModel):
    docx_path: str
    session_id: Optional[str] = None  # v2: 可选 — 指定 session_id 时, style profile 写到 session_dir/style_profiles/


class CompileRequest(BaseModel):
    docx_path: str
    output_path: str
    actions: List[Dict[str, Any]]
    markdown_path: Optional[str] = None
    style_profile_path: Optional[str] = None
    style_mapping: Optional[Dict[str, str]] = None


class DiffRequest(BaseModel):
    before_docx: str
    after_docx: str
    marker_prefix: Optional[str] = ""


# === v2 HTTP 控制面: 3 个 endpoint (会话元数据) ===
# 避坑 3: 列表/删除走 HTTP, WS 不承载 (消除 in-band 查询消息 race)
# 设计: 这 3 个端点全是无状态, 前端用 fetch 调, sidebar 打开时拉一次, 删是用户行为
# 注: /api/upload (v2 Step 3 加) 已在 v2.1 移除 — 前端无上传入口, 避开 Next.js dev server
#     multipart rewrites 不稳的风险

@app.get("/api/sessions")
async def api_list_sessions():
    """列出所有 session (按 updatedAt 倒序). 前端 sidebar 打开时调用."""
    sessions = []
    if not SESSIONS_ROOT.exists():
        return sessions
    for d in SESSIONS_ROOT.iterdir():
        if not d.is_dir() or not (d / "metadata.json").exists():
            continue
        try:
            meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
            msg_path = d / "messages.json"
            msg_count = 0
            if msg_path.exists():
                msgs = json.loads(msg_path.read_text(encoding="utf-8"))
                msg_count = len(msgs.get("entries", []))
            # updated_at 解析: 失败时降级为 0 (排到最前)
            updated_ts = 0
            try:
                updated_ts = int(datetime.fromisoformat(meta["updated_at"]).timestamp() * 1000)
            except (KeyError, ValueError):
                pass
            created_ts = updated_ts  # 默认同 updated
            try:
                created_ts = int(datetime.fromisoformat(meta.get("created_at", meta["updated_at"])).timestamp() * 1000)
            except (KeyError, ValueError):
                pass
            sessions.append({
                "id": meta["session_id"],
                "title": meta.get("title", "新会话"),
                "createdAt": created_ts,
                "updatedAt": updated_ts,
                "messageCount": msg_count,
                "workflowState": meta.get("workflow_state"),
            })
        except (json.JSONDecodeError, KeyError, OSError):
            # 跳过损坏的 session 目录 (不阻塞整个列表)
            continue
    # 按 updatedAt 倒序
    sessions.sort(key=lambda s: s["updatedAt"], reverse=True)
    return sessions


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    """拉单个 session 完整快照 (含 messages). 供前端在 WS resume 之前可选预热."""
    session_dir = SESSIONS_ROOT / session_id
    metadata_path = session_dir / "metadata.json"
    if not session_dir.exists() or not metadata_path.exists():
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        msgs = json.loads((session_dir / "messages.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"session 文件读取失败: {e}")
    return {
        "id": meta["session_id"],
        "title": meta.get("title", "新会话"),
        "docxPath": meta.get("docx_path", ""),
        "messages": msgs.get("entries", []),
        "approvalPhase": meta.get("workflow_state") if meta.get("workflow_state") in ("style_review", "md_draft", "word_editing") else None,
        "isWaitingApproval": meta.get("pending_approval", False),
    }


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """级联删除整个 session 目录 (含 drafts/ / style_profiles/ / uploads/ / logs/ / 3 JSON)."""
    session_dir = SESSIONS_ROOT / session_id
    if session_dir.exists():
        # ⚠️ 两边都 resolve 成绝对路径再做 is_relative_to
        # 原因: pathlib.is_relative_to 是字面量比较, 一边相对一边绝对永远 False
        # (mixed path 被解释为 drive-relative, 永远不 relative)
        if not session_dir.resolve().is_relative_to(SESSIONS_ROOT.resolve()):
            raise HTTPException(status_code=400, detail="非法 session_id (越界)")
        shutil.rmtree(session_dir)
        return {"status": "ok", "deleted": session_id}
    # 幂等: 不存在也返回 ok
    return {"status": "ok", "deleted": session_id, "note": "not_found"}


class CreateSessionRequest(BaseModel):
    mode: str = "fill"  # "fill" | "detail_edit"


@app.post("/api/sessions", status_code=201)
async def api_create_session(req: CreateSessionRequest):
    """创建正规 session (含 mode), 返回 session_id。

    用途: 前端上传文件前先调此接口创建 session, 再上传到该 session 的 workspace。
    后续 WS start 可传 session_id 复用此 session。
    """
    if req.mode not in ("fill", "detail_edit"):
        raise HTTPException(status_code=400, detail=f"非法 mode: {req.mode}")
    session_id = f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    session_dir = SESSIONS_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    # 写完整 metadata (与 Agent.save_to_disk 格式一致)
    metadata = {
        "session_id": session_id,
        "title": "新会话",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "docx_path": "",
        "provider": "",
        "model": "",
        "workflow_state": "style_review" if req.mode == "fill" else "detail_edit",
        "session_complete": False,
        "pending_approval": False,
        "mode": req.mode,
    }
    (session_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (session_dir / "messages.json").write_text(
        json.dumps({"session_id": session_id, "system_prompt": "", "entries": [],
                     "total_input_tokens": 0, "last_prompt_tokens": 0},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (session_dir / "workflow.json").write_text(
        json.dumps({"session_id": session_id, "workflow_state": metadata["workflow_state"],
                     "stage_called_tools": {}, "draft_files_written": [], "round_index": 0,
                     "selected_tools": [], "doc_structure_cache": ""},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"session_id": session_id, "mode": req.mode}


@app.get("/api/sessions/{session_id}/drafts")
async def api_list_session_drafts(session_id: str):
    """列出 session 的所有 MD 草稿 (元数据 + 内容). 供前端 md_draft 阶段右栏多文件 tab 用. 只读."""
    # 1. 沙箱校验第一层: 字符黑名单 (挡掉绝大多数恶意输入, 错误消息友好)
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="非法 session_id (含路径分隔符)")
    # 2. 沙箱校验第二层: 解析后绝对路径越界检查 (与 api_delete_session 同款, 纵深防御)
    session_dir = (SESSIONS_ROOT / session_id).resolve()
    if not session_dir.is_relative_to(SESSIONS_ROOT.resolve()):
        raise HTTPException(status_code=400, detail="非法 session_id (越界)")
    if not session_dir.exists():
        return {"files": []}
    drafts_dir = session_dir / "workspace" / "drafts"
    if not drafts_dir.exists():
        return {"files": []}
    # 3. 遍历 *.md
    #    排序键: (mtime, name) tuple
    #    - 主键 mtime: LLM 按时间顺序写, 顺序对应文档区域流
    #    - 兜底 name : 防 mtime 抖动 (Python 同进程连续 write_text + stat 在
    #                 Windows 上 mtime 可能落到同一整数秒, 顺序会乱飘)
    #    tuple 排序是 Python 标准库级别稳定的, 不会出现"时间一样的两文件
    #    互相交换位置"的情况
    files = []
    for md_path in sorted(
        drafts_dir.glob("*.md"),
        key=lambda p: (p.stat().st_mtime, p.name),
    ):
        stat = md_path.stat()
        files.append({
            "name": md_path.name,
            "content": md_path.read_text(encoding="utf-8"),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime * 1000),  # ms, 与前端 Date.now() 对齐
        })
    return {"files": files}


@app.post("/api/word/compile")
async def compile_word(req: CompileRequest):
    try:
        result_json = markdown_to_word(
            docx_path=req.docx_path,
            output_path=req.output_path,
            actions=req.actions,
            markdown_path=req.markdown_path,
            style_profile_path=req.style_profile_path,
            style_mapping=req.style_mapping
        )
        return json.loads(result_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/word/diff")
async def word_diff(req: DiffRequest):
    try:
        result_json = diff_docx(
            before_docx=req.before_docx,
            after_docx=req.after_docx,
            marker_prefix=req.marker_prefix
        )
        return json.loads(result_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# v3: 实时 DOCX 预览端点 — 走 workspace 沙箱, 强制带 session_id
# 设计:
#   - 替换原 /api/download (死代码, grep 确认无前端调用)
#   - 必须 session_id + path, 走 resolve_workspace_path 防 LLM 幻觉路径
#   - Cache-Control: no-store, 前端用 ?v=<mtime> 自己控缓存
@app.get("/api/word/preview")
async def word_preview(session_id: str, path: str):
    """实时预览 docx 文件 (供 docx-preview 浏览器端渲染).

    路径: /api/word/preview?session_id=<id>&path=<workspace_rel>
    沙箱: resolve_workspace_path(session_id, path, must_exist=True, must_be_file=True)
    响应: 200 application/vnd.openxmlformats-officedocument.wordprocessingml.document
    错误:
      - 400 (session_id 非法 / 路径越界)
      - 404 (文件不存在)
      - 500 (其他异常)
    """
    try:
        resolved = resolve_workspace_path(
            session_id, path, must_exist=True, must_be_file=True
        )
    except WorkspacePathError as e:
        # 区分错误码: 非法 session_id / 越界 / 不存在
        status_map = {
            "name_invalid": 400,
            "absolute": 400,
            "traversal": 400,
            "out_of_root": 400,
            "not_found": 404,
            "not_file": 400,
            "symlink": 400,
        }
        status_code = status_map.get(e.code, 400)
        raise HTTPException(status_code=status_code, detail=e.user_message)
    return FileResponse(
        path=resolved,
        filename=resolved.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Cache-Control": "no-store"},
    )


async def _handle_side_effect(websocket, agent, client_msg: dict) -> dict:
    """侧效消息过滤: 处理不改变 agent 状态机的 WS 消息。

    用途: 用户在 wait_approval / paused 状态时发的"切换流式/非流式"等
    纯侧效消息不应进入 on_user_feedback(后者会清 _pending_approval
    / _pending_feedback 破坏 wait 状态)。

    当前处理的侧效消息:
        - "set_stream_mode": 切换流式/非流式, 调用 agent.set_stream_mode()
                              并回 "stream_mode_changed" 事件, 然后等下一条
                              真正的审批/continue 消息

    返回: 过滤后剩下的 client_msg(供 on_user_feedback 处理)
    """
    if client_msg.get("type") == "set_stream_mode":
        agent.set_stream_mode(client_msg.get("stream_mode", True))
        await websocket.send_json({
            "type": "stream_mode_changed",
            "stream_mode": agent._stream_mode,
        })
        # 继续等真正的审批/continue/feedback 消息
        return await websocket.receive_json()
    return client_msg


@app.websocket("/api/ws/agent")
async def ws_agent(websocket: WebSocket):
    """WebSocket 接入层：转发事件给 Agent，接收用户反馈

    v2 startup 协议 (避坑 3: HTTP/WS 分离):
      - 前→后:  {type: "start", prompt, docx_path}        新建会话
      - 前→后:  {type: "resume", session_id}             恢复会话
      - 后→前:  {type: "session_created", ...}           start 成功
      - 后→前:  {type: "history", messages, ...}         resume 成功
      - 后→前:  {type: "error", message}                 失败
    现有对话事件流 (round_start/reasoning/content/tool_start/tool_end/wait_approval/done) 0 改动.
    """
    await websocket.accept()
    adapter = LLMClientAdapter()
    model = adapter.get_model_name()

    msg_mgr = MessageManager(SYSTEM_PROMPT, token_threshold=150_000)
    msg_mgr.reset()

    # v2: 抽出 startup 解析 + Agent 创建, 让主循环只管"已经有一个 agent, 跑 step()"
    try:
        init_data = await websocket.receive_json()
        init_type = init_data.get("type")

        # === startup 命令分发 (3 种) ===
        if init_type == "start":
            agent, error_msg = await _start_new_session(init_data, adapter, model, msg_mgr)
            if error_msg:
                await websocket.send_json({"type": "error", "message": error_msg})
                await websocket.close()
                return

            # 第一个 frame: session_created
            await websocket.send_json({
                "type": "session_created",
                "session_id": agent.session_id,
                "docx_path": agent.docx_path,
                "approvalPhase": None,
                "isWaitingApproval": False,
                "mode": agent.mode,  # v3
            })

        elif init_type == "resume":
            agent, error_msg = await _resume_existing_session(init_data, adapter)
            if error_msg:
                await websocket.send_json({"type": "error", "message": error_msg})
                await websocket.close()
                return

            # v3 Bug C 修复: 标记下次 step() 是 resume, yield paused 不调 LLM
            agent._is_resume = True

            # 第一个 frame: history (前端用 messages 恢复, 用 approvalPhase/isWaitingApproval 还原按钮)
            # v3 Bug B 修复: messages 用 UI 格式而非后端 OpenAI 格式
            await websocket.send_json({
                "type": "history",
                "session_id": agent.session_id,
                "docxPath": agent.docx_path,
                "messages": _to_ui_messages(agent.msg_mgr._entries),
                "approvalPhase": agent.workflow_state if agent.workflow_state in ("style_review", "md_draft", "word_editing") else None,
                "isWaitingApproval": agent._pending_approval,
                "mode": agent.mode,  # v3
            })

        else:
            await websocket.send_json({"type": "error", "message": "必须先发送 'start' 或 'resume' 命令"})
            await websocket.close()
            return

        # === heartbeat (startup 成功后启动) ===
        async def send_heartbeat():
            while True:
                await asyncio.sleep(15)
                try:
                    await websocket.send_json({"type": "heartbeat"})
                    await asyncio.sleep(0)
                except:
                    break

        heartbeat_task = asyncio.create_task(send_heartbeat())

        # === 主事件循环 (v3: start vs resume 分流) ===
        try:
            if init_type == "resume":
                # === resume 模式: 外层 while True + 内层 async for ===
                # 设计:
                #   - 内层 async for event in agent.step(): 跑一次 step()
                #     · 第一次 _is_resume=True → yield paused → step() return
                #       → async for 自然结束
                #     · 第二次 (paused 后收到用户消息) → 进 while 循环 → 调 LLM
                #   - 每次 paused 后: 等用户消息 → on_user_feedback → break
                #   - 每次 wait_approval 后: 等用户审批 → on_user_feedback → 继续 step()
                #   - done: return 完结
                while True:
                    async for event in agent.step():
                        await websocket.send_json(event)
                        if event["type"] == "wait_approval":
                            # v2: 侧效消息(流式/非流式切换) 在 server 层先吃掉, 不污染 agent 状态机
                            client_res = await websocket.receive_json()
                            client_res = await _handle_side_effect(websocket, agent, client_res)
                            agent.on_user_feedback(client_res)
                            # 继续 step() 走下一步 (会再次进到 while 开头)
                        elif event["type"] == "paused":
                            # 等用户主动发消息 (continue / approve / feedback)
                            client_msg = await websocket.receive_json()
                            client_msg = await _handle_side_effect(websocket, agent, client_msg)
                            agent.on_user_feedback(client_msg)
                            break  # 退出内层 async for, 回到外层 while 重新进入 step()
                        elif event["type"] == "done":
                            return  # 完结
                    else:
                        # 内层 async for 自然结束 (step() return 但没 yield 终止事件)
                        # 防御性: 不应该到这里, 因为 paused/wait_approval/done 都会 break/return
                        return
            else:
                # === start 模式: 原逻辑 0 改动 ===
                async for event in agent.step():
                    await websocket.send_json(event)
                    if event["type"] == "wait_approval":
                        # v2: 侧效消息(流式/非流式切换) 在 server 层先吃掉, 不污染 agent 状态机
                        client_res = await websocket.receive_json()
                        client_res = await _handle_side_effect(websocket, agent, client_res)
                        agent.on_user_feedback(client_res)
        except WebSocketDisconnect:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            print("WebSocket 连接断开")

    except WebSocketDisconnect:
        print("WebSocket 连接断开 (startup 阶段)")
    except Exception as e:
        print(f"WebSocket 异常: {str(e)}")
        try:
            await websocket.send_json({"type": "error", "message": f"系统内部异常: {str(e)}"})
        except:
            pass


# === v3 Bug B 修复: 后端 OpenAI 格式 → 前端 UI 格式转换 ===
def _to_ui_messages(entries: list[dict]) -> list[dict]:
    """把后端 _entries (OpenAI 协议格式) 转换成前端 UI 格式 messages.

    前后端 messages 数组不同构:
      - 后端 _entries: {role, content, tool_calls, tool_call_id} (OpenAI 协议)
      - 前端 Message:  {role, content, reasoning_content, toolName, toolArgs,
                        toolResult, toolStatus, id} (UI 友好)
    这个函数在 history frame 推送前调用, 把后端格式转换为前端格式.

    转换规则:
      - user                  → {role, content, id}
      - assistant             → {role, content?, reasoning_content?, id}
      - assistant(tool_calls) → 同上 (tool_calls 字段不暴露给 UI)
      - tool(tool_call_id)    → {role, toolName, toolArgs, toolResult, toolStatus, id}
                                 通过 tool_call_id 反查 assistant(tool_calls) 提取 name/args
                                 通过 content JSON 提取 result + 判断 status
    """
    # 先建立 tool_call_id → {name, args, result, status} 元数据
    tc_meta: dict = {}
    for entry in entries:
        if entry.get("role") == "assistant" and entry.get("tool_calls"):
            for tc in entry["tool_calls"]:
                tc_id = tc.get("id")
                if not tc_id:
                    continue
                fn = tc.get("function") or {}
                tc_meta[tc_id] = {
                    "name": fn.get("name", "unknown"),
                    "args": fn.get("arguments", ""),
                    "result": None,
                    "status": "running",
                }
        elif entry.get("role") == "tool":
            tc_id = entry.get("tool_call_id")
            if tc_id and tc_id in tc_meta:
                # 解析 content JSON 提取 result + 判断 status
                content_str = entry.get("content") or ""
                try:
                    result_obj = json.loads(content_str)
                    tc_meta[tc_id]["result"] = json.dumps(result_obj, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    # content 不是 JSON, 原样存
                    tc_meta[tc_id]["result"] = content_str
                # status 判断: OpenAI 协议里 tool result 通常包含 "status" 字段
                # error detection: 包含 "error" 关键字
                result_str = tc_meta[tc_id]["result"] or ""
                tc_meta[tc_id]["status"] = "error" if ('"status": "error"' in result_str or '"error"' in result_str) else "success"

    # 折叠成 UI messages 列表
    ui_messages: list[dict] = []
    for entry in entries:
        role = entry.get("role")
        if role == "user":
            ui_messages.append({
                "role": "user",
                "content": entry.get("content", ""),
                "id": f"user-{len(ui_messages)}",
            })
        elif role == "assistant":
            msg: dict = {
                "role": "assistant",
                "id": f"assistant-{len(ui_messages)}",
            }
            if entry.get("content"):
                msg["content"] = entry["content"]
            if entry.get("reasoning_content"):
                msg["reasoning_content"] = entry["reasoning_content"]
            ui_messages.append(msg)
        elif role == "tool":
            tc_id = entry.get("tool_call_id")
            meta = tc_meta.get(tc_id, {})
            name = meta.get("name", "unknown")
            ui_messages.append({
                "role": "tool",
                "toolName": name,
                "toolArgs": meta.get("args", ""),
                "toolResult": meta.get("result", ""),
                "toolStatus": meta.get("status", "success"),
                "id": f"{name}_{len(ui_messages)}",
            })
        # 其他 role (system 等) 不推给前端
    return ui_messages


async def _start_new_session(init_data: dict, adapter: LLMClientAdapter, model: str, msg_mgr: MessageManager):
    """v2: 处理 {type: "start", prompt, docx_path, stream_mode?}.

    Returns: (agent, error_msg)
      - 成功: (Agent, None)
      - 失败: (None, "错误消息")

    v2 扩展: 新增可选 stream_mode 字段(默认 True) — 让前端能指定新会话的初始模式,
    商汤用户可在新建会话前把 toggle 拨到"非流式",新会话直接以非流式启动,
    避免触发 SSE stall 后再切的体验问题。
    """
    user_prompt = (init_data.get("prompt") or "").strip()
    docx_path = init_data.get("docx_path") or ""
    stream_mode = bool(init_data.get("stream_mode", True))  # 默认流式
    mode = init_data.get("mode", "fill")  # v3: "fill" (三阶段填充) | "detail_edit" (细致编辑)
    pre_session_id = (init_data.get("session_id") or "").strip()  # v3: 复用已创建的 session

    if not user_prompt:
        return None, "参数 prompt 不能为空"

    # v3: 复用已创建的 session (如上传文件时通过 POST /api/sessions 创建的)
    if pre_session_id:
        session_id = pre_session_id
        session_dir = SESSIONS_ROOT / session_id
        if not session_dir.exists() or not (session_dir / "metadata.json").exists():
            return None, f"session {session_id} not found"
    else:
        session_id = f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        session_dir = SESSIONS_ROOT / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

    log_path = create_log_file(session_dir)
    provider = adapter.get_provider()
    start_config = {
        "session_id": session_id,  # v2: 启动配置里加 session_id, 方便日志关联
        "provider": provider,
        "model": model,
        "tool_count": len(TOOLS_SCHEMA),
        "interface": "websocket_api",
        "docx_path": docx_path,
        "mode": mode,  # v3: 工作模式
    }
    # Step 6: 通用读取 adapter 状态(替代旧 if-provider 分支)
    # 任何 provider 只要 thinking_type/reasoning_effort 有效就记录, 不再需要按名分支
    thinking_type = adapter.get_thinking_type()
    if thinking_type and thinking_type != "disabled":
        start_config["thinking_type"] = thinking_type
    reasoning_effort = adapter.get_reasoning_effort()
    if reasoning_effort:
        start_config["reasoning_effort"] = reasoning_effort

    append_log(log_path, "启动配置 (Web 终端)", start_config)
    append_log(log_path, "用户输入", user_prompt)

    msg_mgr.append_user(user_prompt)

    agent = Agent(
        system_prompt=SYSTEM_PROMPT,
        llm_adapter=adapter,
        msg_mgr=msg_mgr,
        docx_path=docx_path,
        log_path=log_path,
        session_id=session_id,  # v2
        session_dir=session_dir,  # v2
        stream_mode=stream_mode,  # v2 扩展: 新会话初始流式/非流式模式
        mode=mode,  # v3: 工作模式
    )

    # v2 fix: Agent 创建后**立即**同步写盘, 让 /api/sessions 立即能看到这个 session
    # (否则要等 _checkpoint() fire-and-forget 在 round_start 后异步落盘, LLM 慢的话几秒到十几秒延迟)
    agent.save_to_disk()

    return agent, None


async def _resume_existing_session(init_data: dict, adapter: LLMClientAdapter):
    """v2: 处理 {type: "resume", session_id, stream_mode?}.

    Returns: (agent, error_msg)
      - 成功: (Agent, None) - 从 out/sessions/<id>/ 反序列化
      - 失败: (None, "错误消息")

    v2 扩展: 接受可选 stream_mode(默认 True)— 让用户能控制恢复后的会话从哪种模式起跑。
    注意: stream_mode 当前不持久化到 metadata, 所以 load_from_disk 总是从默认 True 开始,
    恢复后用 init_data.stream_mode 覆盖(如果有)。这样用户即使把旧 session 恢复到非流式也可以。
    """
    session_id = (init_data.get("session_id") or "").strip()
    if not session_id:
        return None, "resume 必须传 session_id"

    session_dir = SESSIONS_ROOT / session_id
    metadata_path = session_dir / "metadata.json"
    if not session_dir.exists() or not metadata_path.exists():
        return None, f"session {session_id} not found"

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    log_path = create_log_file(session_dir)  # 续写同一个 session 的 logs/
    append_log(log_path, "恢复会话 (Web 终端)", {
        "session_id": session_id,
        "workflow_state": metadata.get("workflow_state"),
    })

    agent = Agent.load_from_disk(
        session_dir=session_dir,
        llm_adapter=adapter,
        system_prompt=SYSTEM_PROMPT,
        docx_path=metadata.get("docx_path", ""),
        log_path=log_path,
    )
    # v2 扩展: 恢复后用前端传入的 stream_mode 覆盖(load_from_disk 默认 True)
    if "stream_mode" in init_data:
        agent.set_stream_mode(bool(init_data["stream_mode"]))
    return agent, None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)