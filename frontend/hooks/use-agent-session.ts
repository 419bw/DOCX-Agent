"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import type { Message } from "../lib/message-types";
import type { DocxPreviewReady } from "../lib/docx-preview-types";

export type ApprovalPhase = "style_review" | "md_draft" | "word_editing" | null;

export interface CurrentSessionInfo {
  id: string;
  docxPath: string;
  approvalPhase: ApprovalPhase;
  isWaitingApproval: boolean;
}

const CURRENT_SESSION_KEY = "docx-agent:currentSessionId";

// === v3.2 helper: 从 messages[] 找最近成功的 markdown_to_word 结果 ===
// 用途: 切到 word_editing 阶段时, 后端没推 docx_preview_ready (那是事件级),
//       但 history 里的 messages[] 含有最近一次 markdown_to_word 的成功 result.
//       我们合成一个 DocxPreviewReady 喂给 panel, 立刻显示上次编辑结果 (而不是空).
function findLastMarkdownToWordResult(messages: Message[]): DocxPreviewReady | null {
  // 从末尾向前找, 第一个 status="success" 的 markdown_to_word
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (
      msg.role === "tool" &&
      msg.toolName === "markdown_to_word" &&
      msg.toolStatus === "success" &&
      msg.toolResult
    ) {
      try {
        const result = JSON.parse(msg.toolResult) as {
          status?: string;
          docx_path?: string;
          output_path?: string;
          action_count?: number;
          diagnostics?: DocxPreviewReady["diagnostics"];
          support_summary?: DocxPreviewReady["support_summary"];
        };
        if (
          result.status === "ok" &&
          result.docx_path &&
          result.output_path
        ) {
          return {
            type: "docx_preview_ready",
            preview_path: result.output_path,
            input_path: result.docx_path,
            // PoC 简化: 不查真实 mtime, 用 Date.now() 保证首次渲染触发.
            // 后续新 markdown_to_word 事件会用真实 mtime 覆盖, 自然 bust 缓存.
            // 服务端 endpoint 也设了 Cache-Control: no-store, HTTP 层不缓存.
            docx_mtime_ms: Date.now(),
            // 没有 diff 数据 (历史快照), 等下次 markdown_to_word 才有真实 diff
            paragraph_changes: [],
            changed_files: [],
            diagnostics: result.diagnostics ?? [],
            action_count: result.action_count ?? 0,
            support_summary: result.support_summary ?? { native: 0, degraded: 0, rejected: 0 },
          };
        }
      } catch {
        // 解析失败, 继续往前找
      }
    }
  }
  return null;
}

/**
 * useAgentSession — 封装 WebSocket 生命周期 + onmessage 状态机 + 实时流 refs
 *
 * 从原 page.tsx 1:1 提取, 函数体零修改。
 * 通过 callback props 处理 page UI 侧栏联动 (showPreview / drafts / sessions),
 * 不让 hook 越界管理 UI state。
 */
export function useAgentSession(opts: {
  onRefreshSessions: () => void;
  onFetchDrafts: (sessionId: string) => void;
  onResetDrafts: () => void;
  onShowPreview: (show: boolean) => void;
  onDocxPreviewReady?: (info: DocxPreviewReady) => void;  // v3: 实时 DOCX 预览事件回调
}) {
  const { onRefreshSessions, onFetchDrafts, onResetDrafts, onShowPreview, onDocxPreviewReady } = opts;

  // === Agent state (从 page.tsx 1:1 搬入) ===
  const [messages, setMessages] = useState<Message[]>([]);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isWaitingApproval, setIsWaitingApproval] = useState<boolean>(false);
  const [approvalPhase, setApprovalPhase] = useState<ApprovalPhase>(null);
  const [docxPath, setDocxPath] = useState<string>("");
  const [isGenerating, setIsGenerating] = useState<boolean>(false);
  const [tokenCount, setTokenCount] = useState<number>(0);

  // === 流式 / 非流式 开关 (v2 修复 SenseNova SSE stall) ===
  // 用途: 商汤等 SSE 协议层有 bug 的 provider, 用户在 stalled 时切到非流式
  // 让下一轮 step() 走 create_chat_completion_blocking()
  // 状态: 由后端 stream_mode_changed 帧校正 (单 source of truth = 后端)
  // 默认 False (非流式) — 默认不走流式
  const [streamMode, setStreamMode] = useState<boolean>(false);

  // === 实时流状态 (RAF 节流) ===
  const [liveReasoning, setLiveReasoning] = useState<string>("");
  const [liveContent, setLiveContent] = useState<string>("");
  const [thinkTime, setThinkTime] = useState<number>(0);

  // === 实时流 Refs ===
  const liveReasoningRef = useRef<string>("");
  const liveContentRef = useRef<string>("");
  const isRenderingRef = useRef<boolean>(false);  // RAF 节流锁
  const thinkTimerRef = useRef<NodeJS.Timeout | null>(null);
  const pendingPromptRef = useRef<string>("");

  const wsRef = useRef<WebSocket | null>(null);

  // === 会话管理 (v2: 后端持久化, 前端只维护"当前激活的 session_id") ===
  const [currentSessionId, setCurrentSessionIdState] = useState<string | null>(null);
  const [currentSessionInfo, setCurrentSessionInfo] = useState<CurrentSessionInfo | null>(null);

  // v3 修复: 同时维护 currentSessionIdRef, 解决 socket.onmessage 闭包陈旧问题
  //   socket.onmessage 在 start 首次调用时绑定, 捕获**那一刻**的
  //   currentSessionId (新会话下是 null); 后续 setCurrentSessionId 触发重渲染
  //   不会重新绑定 onmessage, 闭包仍是 null → fetchDrafts 条件失败
  //   ref 永远是最新的, 用于 onmessage 闭包内读取
  const currentSessionIdRef = useRef<string | null>(null);

  // === v2: localStorage 单 key helper (只存"上次激活的 session_id", 不存 session 内容) ===
  const setCurrentSessionId = useCallback((id: string | null) => {
    if (typeof window === "undefined") return;
    try {
      if (id === null) localStorage.removeItem(CURRENT_SESSION_KEY);
      else localStorage.setItem(CURRENT_SESSION_KEY, id);
      setCurrentSessionIdState(id);
    } catch (e) {
      console.warn("setCurrentSessionId failed:", e);
    }
    // 同步更新 ref (绕过闭包陷阱)
    currentSessionIdRef.current = id;
  }, []);

  // === v3: 实时 DOCX 预览状态 (markdown_to_word 完成后由后端推 docx_preview_ready) ===
  const [docxPreviewInfo, setDocxPreviewInfo] = useState<DocxPreviewReady | null>(null);

  // === v3: 细致编辑模式 ===
  const [editMode, setEditMode] = useState<"fill" | "detail_edit">("fill");
  const [isSelectingTools, setIsSelectingTools] = useState<boolean>(false);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);

  // === 实时流清理 ===
  const clearLiveStream = useCallback(() => {
    liveReasoningRef.current = "";
    liveContentRef.current = "";
    setLiveReasoning("");
    setLiveContent("");
    setThinkTime(0);
    if (thinkTimerRef.current) {
      clearInterval(thinkTimerRef.current);
      thinkTimerRef.current = null;
    }
  }, []);

  // === resetForCreate — handleCreateSession 用的全量重置 ===
  //   含: messages, liveStream, docxPath, isWaitingApproval, approvalPhase,
  //       isGenerating, isConnected, tokenCount, currentSessionId, currentSessionInfo, 关 WS
  //   不含: page UI state (feedbackValue, expandedTools, selectedToolId, showPreview, sessions, ...)
  //   v2: 不重置 streamMode — 让用户在新 session 启动前先把 toggle 拨到"非流式",
  //   新 session 直接以非流式启动,避免触发 SSE stall 后再切的体验问题
  const resetForCreate = useCallback(() => {
    setMessages([]);
    clearLiveStream();
    setDocxPath("");
    setIsWaitingApproval(false);
    setApprovalPhase(null);
    setIsGenerating(false);
    setIsConnected(false);
    setTokenCount(0);
    setCurrentSessionId(null);
    setCurrentSessionInfo(null);
    setDocxPreviewInfo(null);  // v3: 切会话时清空预览
    pendingPromptRef.current = "";
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, [clearLiveStream, setCurrentSessionId]);

  // === resetForWorkspace — resetWorkspace 用的部分重置 ===
  //   原 resetWorkspace 不重置 isConnected / tokenCount / currentSessionId / currentSessionInfo
  //   (因为它是"清空 UI", 不是"销毁 session")
  //   v2: 同 resetForCreate, 不重置 streamMode (切会话时也保留 toggle 状态)
  const resetForWorkspace = useCallback(() => {
    setMessages([]);
    clearLiveStream();
    setDocxPath("");
    setIsWaitingApproval(false);
    setApprovalPhase(null);
    setIsGenerating(false);
    setDocxPreviewInfo(null);  // v3: 切会话时清空预览
    pendingPromptRef.current = "";
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, [clearLiveStream]);

  // === 关 WS ===
  const stop = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  // === 是否有活跃连接 ===
  const hasActiveConnection = useCallback(() => {
    return wsRef.current !== null && wsRef.current.readyState === WebSocket.OPEN;
  }, []);

  // === sendContinue — 续发 (新 prompt 走 WS continue 帧) ===
  const sendContinue = useCallback((prompt: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return false;
    }
    setMessages((prev) => [...prev, { role: "user", content: prompt }]);
    setIsGenerating(true);
    wsRef.current.send(JSON.stringify({ type: "continue", prompt }));
    return true;
  }, []);

  // === sendApprove — 审批 (approved / reject 走 WS approve 帧) ===
  const sendApprove = useCallback((approved: boolean, feedback?: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return false;
    }
    wsRef.current.send(
      JSON.stringify({
        type: "approve",
        approved,
        feedback: feedback || "",
      })
    );
    const userActionText = approved
      ? "【确认同意】同意并进入下一阶段"
      : `【拒绝反馈】反馈修改建议：${feedback}`;
    setMessages((prev) => [...prev, { role: "user", content: userActionText }]);
    setIsWaitingApproval(false);
    setApprovalPhase(null);
    setIsGenerating(true);
    return true;
  }, []);

  // === sendSetStreamMode — 切换流式 / 非流式 ===
  // 行为:
  //   - 乐观更新本地 state (点击立刻有反馈, 不等 round-trip)
  //   - WS 已 OPEN 时发 set_stream_mode 消息, 后端回 stream_mode_changed 帧校正
  //   - WS 未连接时(新对话空状态 / 重连中)只更新本地; 下次 start 帧会带新值
  //     (start 帧构造处直接读 streamMode, 见下方 start()), 不会丢
  // 返回值: 本地状态是否已更新(总是 true, 兼容旧调用方 page.tsx 不看返回值)
  const sendSetStreamMode = useCallback((mode: boolean) => {
    setStreamMode(mode);  // 乐观更新 — 总是先执行, 与 WS 状态解耦
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: "set_stream_mode", stream_mode: mode })
      );
    }
    return true;
  }, []);

  // === start — 启动/恢复 WS session (原 startAgentSession, 函数体 1:1 搬入) ===
  const start = useCallback((initialPrompt: string, path: string, resumeSessionId?: string, mode?: "fill" | "detail_edit", preCreatedSessionId?: string) => {
    if (wsRef.current) {
      wsRef.current.close();
    }

    // 发起新会话: 重置预览侧栏状态
    onShowPreview(false);
    onResetDrafts();

    if (resumeSessionId && initialPrompt) {
      pendingPromptRef.current = initialPrompt;
    } else {
      pendingPromptRef.current = "";
    }

    const socket = new WebSocket("ws://127.0.0.1:8000/api/ws/agent");
    wsRef.current = socket;

    socket.onopen = () => {
      setIsConnected(true);
      if (resumeSessionId) {
        // v2: resume 已有 session (后端 Agent.load_from_disk)
        socket.send(JSON.stringify({
          type: "resume",
          session_id: resumeSessionId,
          stream_mode: streamMode,  // v2 扩展: 恢复时也带当前 toggle 状态
        }));
        // 注意: 不立即 push user message, 等 history frame 回来再覆盖
      } else {
        // v2: start 新 session (后端生成 session_id)
        setIsGenerating(true);
        const sessionMode = mode || "fill";
        setEditMode(sessionMode);
        setSelectedTools([]);
        socket.send(JSON.stringify({
          type: "start",
          prompt: initialPrompt,
          docx_path: path,
          stream_mode: streamMode,  // v2 扩展: 新会话初始流式/非流式模式
          mode: sessionMode,  // v3: 工作模式
          session_id: preCreatedSessionId || undefined,  // v3: 复用已创建的 session
        }));
        if (initialPrompt) {
          setMessages((prev) => [...prev, { role: "user", content: initialPrompt }]);
        }
      }
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);

      switch (data.type) {
        case "session_created": {
          // v2: 后端生成 session_id, 第一个 frame 推过来
          setCurrentSessionId(data.session_id);
          setCurrentSessionInfo({
            id: data.session_id,
            docxPath: data.docx_path || "",
            approvalPhase: data.approvalPhase ?? null,
            isWaitingApproval: data.isWaitingApproval ?? false,
          });
          if (data.mode) setEditMode(data.mode);  // v3
          // 异步刷新 sidebar 列表 (拉新 session 进来)
          onRefreshSessions();
          break;
        }
        case "history": {
          // v2: resume 成功, 后端 dump 完整 history
          setCurrentSessionId(data.session_id);
          setCurrentSessionInfo({
            id: data.session_id,
            docxPath: data.docxPath || "",
            approvalPhase: data.approvalPhase ?? null,
            isWaitingApproval: data.isWaitingApproval ?? false,
          });
          setMessages(data.messages || []);
          setDocxPath(data.docxPath || "");
          setApprovalPhase(data.approvalPhase ?? null);
          setIsWaitingApproval(data.isWaitingApproval ?? false);
          if (data.mode) setEditMode(data.mode);  // v3
          // 状态恢复后, 不需要再等后端推内容 (history 已经是终态)
          setIsGenerating(false);
          onRefreshSessions();
          // 切到 md_draft 阶段的 session: 自动拉取其草稿文件列表
          // (切会话时不会触发 wait_approval, 所以必须在 resume 阶段补一刀)
          // v3: 切到 word_editing 阶段也展开预览面板 + 拉草稿
          // (word_editing 阶段没有 wait_approval, 唯一机会就是 history 帧)
          if (data.approvalPhase === "md_draft" || data.approvalPhase === "word_editing") {
            onShowPreview(true);
            onFetchDrafts(data.session_id);
          }
          // v3.2: word_editing resume 时, 从 messages[] 里找最近成功的 markdown_to_word
          //        合成一个 DocxPreviewReady, 让 panel 立刻显示上次编辑结果
          //        (而不是等下次 markdown_to_word 才有内容)
          if (data.approvalPhase === "word_editing") {
            const syntheticPreview = findLastMarkdownToWordResult(data.messages || []);
            if (syntheticPreview) {
              setDocxPreviewInfo(syntheticPreview);
            }
          }
          break;
        }
        case "round_start": {
          // 修复 B: 先把当前 ref 里的内容固化为 messages (防止"被后端打回"时数据丢失)
          // 后端在 LLM 漏调/错调工具时会丢弃上一轮 reasoning+content, 直接发 round_start
          // 此时如果没有兜底固化, 用户会看到内容"突然消失"
          const prevR = liveReasoningRef.current;
          const prevC = liveContentRef.current;
          if (prevR || prevC) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: prevC || undefined,
                reasoning_content: prevR || undefined,
              },
            ]);
          }
          // 然后才清空
          liveReasoningRef.current = "";
          liveContentRef.current = "";
          setLiveReasoning("");
          setLiveContent("");
          setThinkTime(0);

          if (thinkTimerRef.current) clearInterval(thinkTimerRef.current);
          thinkTimerRef.current = setInterval(() => {
            setThinkTime((prev) => prev + 1);
          }, 1000);

          setIsGenerating(true);
          if (data.token_count !== undefined) {
            setTokenCount(data.token_count);
          }
          break;
        }

        case "heartbeat":
          break;

        case "stream_mode_changed": {
          // v2: 后端回执, 校正本地 streamMode (单 source of truth = 后端)
          setStreamMode(Boolean(data.stream_mode));
          break;
        }

        case "reasoning": {
          // RAF 节流
          liveReasoningRef.current += data.delta;
          if (!isRenderingRef.current) {
            isRenderingRef.current = true;
            requestAnimationFrame(() => {
              setLiveReasoning(liveReasoningRef.current);
              isRenderingRef.current = false;
            });
          }
          break;
        }

        case "content": {
          liveContentRef.current += data.delta;
          if (!isRenderingRef.current) {
            isRenderingRef.current = true;
            requestAnimationFrame(() => {
              setLiveContent(liveContentRef.current);
              isRenderingRef.current = false;
            });
          }
          break;
        }

        case "reasoning_end": {
          // 推理结束: 立即固化思考到历史, 让 ReasoningPanel 触发折叠动画
          // 消除"思考-工具调用"之间的视觉黑洞期
          const txtR = liveReasoningRef.current;
          const txtC = liveContentRef.current;
          if (txtR || txtC) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: txtC || undefined,
                reasoning_content: txtR || undefined,
              },
            ]);
          }
          clearLiveStream();
          break;
        }

        case "tool_start": {
          // 读 ref 固化为 messages
          const txtR = liveReasoningRef.current;
          const txtC = liveContentRef.current;
          if (txtR || txtC) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: txtC || undefined,
                reasoning_content: txtR || undefined,
              },
            ]);
          }
          // 延迟一帧清空: 让 messages 推入先 commit, AnimatePresence 看到 exit
          clearLiveStream();
          setMessages((prev) => [
            ...prev,
            {
              role: "tool",
              toolName: data.name,
              toolArgs: data.arguments,
              toolStatus: "running",
              id: data.name + "_" + crypto.randomUUID(),
            },
          ]);
          break;
        }

        case "tool_end": {
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.role === "tool" && msg.toolName === data.name && msg.toolStatus === "running") {
                return {
                  ...msg,
                  toolStatus: data.result.includes('"status": "error"') ? "error" : "success",
                  toolResult: data.result,
                };
              }
              return msg;
            })
          );
          break;
        }

        // v3: 实时 DOCX 预览事件 (markdown_to_word 完成后由后端推)
        case "docx_preview_ready": {
          const info = data as DocxPreviewReady;
          setDocxPreviewInfo(info);
          onDocxPreviewReady?.(info);  // page.tsx 用这个切到 docx tab
          break;
        }

        // v3: 细致编辑模式 — 工具选用事件
        case "tool_selection_start": {
          setIsSelectingTools(true);
          break;
        }
        case "tool_selection_end": {
          setIsSelectingTools(false);
          setSelectedTools(data.selected_tools || []);
          break;
        }

        case "paused": {
          // v3: 切历史后后端 yield 的首个事件, 表示"已恢复状态, 等用户消息"
          // 作用:
          //   - setIsGenerating(false): 关闭 loading 状态
          //   - setApprovalPhase: 还原后端的 workflow_state (style_review/md_draft/word_editing)
          //   - setIsWaitingApproval: 决定 UI 显示"批准/拒绝"按钮还是输入框
          // 注意: messages 已经从 history frame 加载过了, 这里不需要再处理
          clearLiveStream();
          setIsGenerating(false);
          setApprovalPhase(data.phase as any);
          // 字段名错配修复: 后端 paused 帧字段是 snake_case `is_waiting_approval`
          //   (src/agent.py:370), 而 history 帧是 camelCase `isWaitingApproval`
          //   兼容两种命名, 优先 snake (paused 帧用)
          setIsWaitingApproval((data.is_waiting_approval ?? data.isWaitingApproval) as boolean);
          // 状态:
          //   - isWaitingApproval=true  → 显示"批准/拒绝"按钮 (用户发 approve/reject)
          //   - isWaitingApproval=false → 隐藏按钮, 等用户主动发新消息

          // 自动发送暂存的挂起 prompt
          if (pendingPromptRef.current) {
            const prompt = pendingPromptRef.current;
            pendingPromptRef.current = ""; // 清空
            setIsGenerating(true);
            setMessages((prev) => [...prev, { role: "user", content: prompt }]);
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: "continue", prompt }));
            }
          }
          break;
        }

        case "wait_approval": {
          const txtR = liveReasoningRef.current;
          const txtC = liveContentRef.current;
          if (txtR || txtC) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: txtC || undefined,
                reasoning_content: txtR || undefined,
              },
            ]);
          }
          clearLiveStream();

          // 修复 1: 智能 push vs merge
          // 如果最后一条是 assistant, 合并 content; 否则 push 新 assistant
          if (data.content !== undefined) {
            setMessages((prev) => {
              const lastIdx = prev.length - 1;
              if (lastIdx >= 0 && prev[lastIdx].role === "assistant") {
                // 最后一条是 assistant: 合并 content
                return [...prev.slice(0, lastIdx), { ...prev[lastIdx], content: data.content }];
              }
              // 最后一条不是 assistant (如 tool): 安全地 push 新 assistant
              return [...prev, { role: "assistant", content: data.content }];
            });
          }
          setApprovalPhase(data.phase);
          setIsWaitingApproval(true);
          setIsGenerating(false);

          // md_draft 阶段: 自动展开右侧预览侧栏(从后端拉取结构化文件列表)
          // 拉取策略见 fetchDrafts() 注释: 一次拿所有文件, 默认选最新
          // v3 修复: 用 currentSessionIdRef 读最新值, 避免 onmessage 闭包陈旧
          const sessionId = currentSessionIdRef.current;
          if (data.phase === "md_draft" && sessionId) {
            onShowPreview(true);
            onFetchDrafts(sessionId);
          }
          break;
        }

        case "done": {
          const txtR = liveReasoningRef.current;
          const txtC = liveContentRef.current;
          if (txtR || txtC) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: txtC || undefined,
                reasoning_content: txtR || undefined,
              },
            ]);
          }
          clearLiveStream();

          // 修复 1: 同 wait_approval 模式
          if (data.content !== undefined) {
            setMessages((prev) => {
              const lastIdx = prev.length - 1;
              if (lastIdx >= 0 && prev[lastIdx].role === "assistant") {
                return [...prev.slice(0, lastIdx), { ...prev[lastIdx], content: data.content }];
              }
              return [...prev, { role: "assistant", content: data.content }];
            });
          }
          setIsWaitingApproval(false);
          setApprovalPhase(null);
          setIsGenerating(false);
          break;
        }

        case "error":
          setIsGenerating(false);
          alert(`Agent 运行报错: ${data.message}`);
          break;
      }
    };

    socket.onclose = () => {
      setIsConnected(false);
      setIsWaitingApproval(false);
      setApprovalPhase(null);
      setIsGenerating(false);
      if (thinkTimerRef.current) {
        clearInterval(thinkTimerRef.current);
        thinkTimerRef.current = null;
      }
    };

    socket.onerror = (err) => {
      console.error("WebSocket error", err);
      setIsConnected(false);
      setIsWaitingApproval(false);
      setApprovalPhase(null);
      setIsGenerating(false);
    };
  }, [onRefreshSessions, onFetchDrafts, onResetDrafts, onShowPreview, setCurrentSessionId, clearLiveStream, streamMode, onDocxPreviewReady]);

  // === unmount 时关 WS (原 page.tsx 没有 cleanup, 但我们补上避免泄漏) ===
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  return {
    // state
    messages,
    isConnected,
    isGenerating,
    isWaitingApproval,
    approvalPhase,
    docxPath,
    tokenCount,
    liveReasoning,
    liveContent,
    thinkTime,
    currentSessionId,
    currentSessionInfo,
    streamMode,
    docxPreviewInfo,  // v3: 实时 DOCX 预览 (markdown_to_word 完成后由后端推)
    editMode,  // v3: 工作模式 (fill / detail_edit)
    setEditMode,  // v3: 模式切换
    isSelectingTools,  // v3: 工具选用进行中
    selectedTools,  // v3: 当前选用的工具列表
    // setters (供 page handlers 调用)
    setCurrentSessionId,
    // actions
    start,
    stop,
    sendContinue,
    sendApprove,
    sendSetStreamMode,
    clearLiveStream,
    resetForCreate,
    resetForWorkspace,
    hasActiveConnection,
  };
}
