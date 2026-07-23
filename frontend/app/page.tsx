"use client";

import React, { useState, useEffect, useRef } from "react";
import { Terminal } from "lucide-react";
// PanelLeft/Send 已迁到 ChatHeader/ChatInput, RefreshCw/Plus/Trash2 等在 SessionSidebar 内部
// motion/AnimatePresence 已迁到 ChatMessageBlocks/ChatInput
// MarkdownRenderer/ReasoningPanel 已迁入 ChatMessageBlocks
import PreviewPanel from "../components/preview-panel";
import SessionSidebar from "../components/session-sidebar";
import AnimatedLivePanel from "../components/animated-live-panel";
import ChatHeader from "../components/chat-header";
import ChatMessageBlocks from "../components/chat-message-blocks";
import ApprovalCheckpoint from "../components/approval-checkpoint";
import ChatInput from "../components/chat-input";
import WorkspacePanel, { type WorkspaceFile } from "../components/workspace-panel";
// v2: 删 IndexedDB lib/sessions — 改用 HTTP fetch + WS resume (后端是 source of truth)
import type { SessionMeta } from "../lib/session-types";
import type { DraftFile } from "../lib/draft-types";
import type { Message, RenderBlock } from "../lib/message-types";
import { useAgentSession } from "../hooks/use-agent-session";

export default function Home() {
  // === 本地 UI state (不被 hook 管) ===
  const [inputValue, setInputValue] = useState<string>("");
  const [feedbackValue, setFeedbackValue] = useState<string>("");

  // === 草稿预览侧栏 (md_draft 阶段自动展开) ===
  const [showPreview, setShowPreview] = useState<boolean>(false);
  const [draftFiles, setDraftFiles] = useState<DraftFile[]>([]);
  const [activeFilename, setActiveFilename] = useState<string | null>(null);

  // === v3: 预览面板的 tab 模式 (MD 草稿 vs DOCX 实时) ===
  //   默认 "md"; word_editing 阶段收到 docx_preview_ready 时 auto-switch 到 "docx"
  const [previewMode, setPreviewMode] = useState<"md" | "docx">("md");

  // === v2 (Phase 4): 文件工作区 (上传 / 选 active docx / 删除) ===
  const [showWorkspace, setShowWorkspace] = useState<boolean>(false);
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([]);
  const [activeDocxName, setActiveDocxName] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState<boolean>(false);

  // === 会话管理 (v2: 后端持久化, 前端只维护"当前激活的 session_id") ===
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [sessionSidebarOpen, setSessionSidebarOpen] = useState<boolean>(false);

  // === 工具选择 UI state (本地) ===
  const [, setExpandedTools] = useState<Set<string>>(new Set());
  const [selectedToolId, setSelectedToolId] = useState<string | null>(null);

  // === Refs ===
  const chatEndRef = useRef<HTMLDivElement>(null);
  // === 滚动意图侦测 (修复 2) ===
  const isScrolledToBottom = useRef<boolean>(true);

  // === Agent hook (WS 生命周期 + 实时流 + approval state) ===
  //   回调函数让 hook 在 onmessage 处理时能触发 page UI 侧栏联动
  //   (hook 不越界管 UI state, 保持单一职责)
  const agent = useAgentSession({
    onRefreshSessions: () => { void refreshSessions(); },
    onFetchDrafts: (sessionId) => { void fetchDrafts(sessionId); },
    onResetDrafts: () => resetDrafts(),
    onShowPreview: (show) => setShowPreview(show),
    onDocxPreviewReady: (info) => {
      // v3: 收到 markdown_to_word 完成事件 → auto-switch 到 DOCX tab + 展开预览
      setPreviewMode("docx");
      setShowPreview(true);
    },
  });
  const {
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
    setCurrentSessionId,
    streamMode,
    docxPreviewInfo,  // v3: 实时 DOCX 预览事件
    editMode,  // v3: 工作模式
    setEditMode,  // v3: 模式切换
    isSelectingTools,  // v3: 工具选用中
    selectedTools,  // v3: 选用工具列表
    start: startAgentSession,
    stop: stopAgentSession,
    sendContinue,
    sendApprove,
    sendSetStreamMode,
    resetForCreate,
    resetForWorkspace,
    hasActiveConnection,
  } = agent;

  // === v2.2: 启动时只拉列表 (sidebar 用), 不自动 resume 任何 session ===
  // 用户期望: 进页面默认空 UI, 由用户主动从 sidebar 选 / 新建
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/sessions");
        if (res.ok) {
          const list: SessionMeta[] = await res.json();
          setSessions(list);
        } else {
          console.warn("fetch /api/sessions failed:", res.status);
          setSessions([]);
        }
      } catch (e) {
        console.warn("session list fetch error:", e);
        setSessions([]);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // === v2: 重新拉列表 (删除/创建后用) ===
  const refreshSessions = async () => {
    try {
      const res = await fetch("/api/sessions");
      if (res.ok) {
        const list: SessionMeta[] = await res.json();
        setSessions(list);
      }
    } catch (e) {
      console.warn("refreshSessions failed:", e);
    }
  };

  // === v2: 拉取指定 session 的所有 MD 草稿 (元数据 + 内容) ===
  const fetchDrafts = async (sessionId: string) => {
    if (!sessionId) return;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/drafts`);
      if (res.ok) {
        const data: { files: DraftFile[] } = await res.json();
        setDraftFiles(data.files);
        setActiveFilename((prev) => {
          if (prev && data.files.some((f) => f.name === prev)) return prev;
          const latest = data.files[data.files.length - 1];
          return latest?.name ?? null;
        });
      } else {
        console.warn("fetchDrafts failed:", res.status);
      }
    } catch (e) {
      console.warn("fetchDrafts error:", e);
    }
  };

  // === 清空草稿列表 + 选中 (切会话 / 新建 / 重置时复用) ===
  const resetDrafts = () => {
    setDraftFiles([]);
    setActiveFilename(null);
  };

  // === v2 (Phase 4): Workspace 文件工作区 — 拉取/上传/删除/设 active ===
  const fetchWorkspace = async (sessionId: string) => {
    if (!sessionId) return;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/workspace`);
      if (res.ok) {
        const data: { files: WorkspaceFile[] } = await res.json();
        setWorkspaceFiles(data.files);
      } else {
        console.warn("fetchWorkspace failed:", res.status);
        setWorkspaceFiles([]);
      }
    } catch (e) {
      console.warn("fetchWorkspace error:", e);
      setWorkspaceFiles([]);
    }
  };

  const uploadToWorkspace = async (sessionId: string, file: File) => {
    const formData = new FormData();
    formData.append("files", file);
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/upload`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `upload failed: ${res.status}`);
    }
    await fetchWorkspace(sessionId);
  };

  const deleteFromWorkspace = async (sessionId: string, name: string) => {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/workspace/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (!res.ok && res.status !== 404) {
      console.warn("deleteWorkspaceFile failed:", res.status);
    }
    await fetchWorkspace(sessionId);
    // 删的是 active docx, 清除 active 状态
    setActiveDocxName((prev) => (prev === name ? null : prev));
  };

  const setActiveDocx = (name: string) => {
    setActiveDocxName(name);
  };

  // workspace 打开时自动拉一次
  useEffect(() => {
    if (showWorkspace && currentSessionId) {
      void fetchWorkspace(currentSessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showWorkspace, currentSessionId]);

  // 切换会话时清空 workspace
  useEffect(() => {
    setWorkspaceFiles([]);
    setActiveDocxName(null);
  }, [currentSessionId]);

  // === v3.2: 阶段切换时, 自动设预览模式 ===
  // word_editing → DOCX tab (默认显示上次编辑结果)
  // md_draft → MD tab (草稿列表是主要内容)
  useEffect(() => {
    if (approvalPhase === "word_editing") {
      setPreviewMode("docx");
    } else if (approvalPhase === "md_draft") {
      setPreviewMode("md");
    }
  }, [approvalPhase]);

  // === v2: 切会话 = 关闭旧 WS + 通过 resume 拉新 (Bug B 完整修复) ===
  const handleSelectSession = (id: string) => {
    if (id === currentSessionId) {
      setSessionSidebarOpen(false);
      return;
    }
    // 1. 关旧 WS
    stopAgentSession();
    // 2. WS resume 重建 (后端发 history frame, onmessage 处理覆盖 state)
    startAgentSession("", "", id);
    setSessionSidebarOpen(false);
  };

  // === v2: 新建会话 = 清空前端 state + 留空等用户发首条消息 (发时走 start) ===
  const handleCreateSession = () => {
    // Bug A 修复: 重置所有 approval + UI 状态, 避免旧 session 的状态泄漏到新 session
    setFeedbackValue("");
    setExpandedTools(new Set());
    setSelectedToolId(null);
    setSessionSidebarOpen(false);
    resetDrafts();
    // 重置 agent state (含 WS 关闭 + 全部 agent state)
    resetForCreate();
    // v2 fix: 新建空 session (前端), 主动刷列表让 sidebar 立即看到
    void refreshSessions();
  };

  // === v2: 删除 = HTTP DELETE + 重新拉列表 + 切到下一个 / 留空 ===
  const handleDeleteSession = async (id: string) => {
    if (!confirm("确认删除该会话? 此操作不可恢复。")) return;
    try {
      const res = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
      if (!res.ok) console.warn("DELETE session failed:", res.status);
    } catch (e) {
      console.warn("delete session error:", e);
    }
    await refreshSessions();
    if (id === currentSessionId) {
      // 删的是当前, 切到下一个或留空
      const remaining = sessions.filter((s) => s.id !== id);
      if (remaining.length > 0) {
        handleSelectSession(remaining[0].id);
      } else {
        handleCreateSession();
      }
    }
  };

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const { scrollTop, scrollHeight, clientHeight } = e.currentTarget;
    // 阈值 150px: 离底部 150px 内算"在底部"
    isScrolledToBottom.current = scrollHeight - scrollTop - clientHeight < 150;
  };

  // Auto-scroll chat window (修复 2: 滚动意图侦测)
  useEffect(() => {
    if (!isScrolledToBottom.current) return;
    chatEndRef.current?.scrollIntoView({
      behavior: isGenerating ? "auto" : "smooth",  // 流式用 auto 避免动画排队
    });
  }, [messages, isWaitingApproval, liveReasoning, liveContent, isGenerating]);


  const handleSendPrompt = (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputValue.trim()) return;
    const prompt = inputValue.trim();
    setInputValue("");

    if (!hasActiveConnection()) {
      isScrolledToBottom.current = true;
      // v3: 有消息历史 → resume (从磁盘加载上下文); 无历史 → start 新建 (传 mode + 可选 session_id)
      if (currentSessionId && messages.length > 0) {
        startAgentSession(prompt, docxPath, currentSessionId);
      } else {
        startAgentSession(prompt, docxPath, undefined, editMode, currentSessionId || undefined);
      }
      return;
    }

    if (isWaitingApproval) return;

    // 修复 2: 用户发了 prompt, 应该跟读
    isScrolledToBottom.current = true;
    if (!sendContinue(prompt)) {
      // WS 在 check 后断了, 尝试恢复
      if (currentSessionId) {
        startAgentSession(prompt, docxPath, currentSessionId);
      } else {
        startAgentSession(prompt, "", undefined, editMode);
      }
    }
  };

  const handleApprove = (approved: boolean, feedback?: string) => {
    if (!sendApprove(approved, feedback)) {
      alert("与 Agent 的连接已断开，请重新输入需求开始新会话。");
      return;
    }
    isScrolledToBottom.current = true;  // 修复 2: 审批完成, 用户应跟读下一阶段
  };

  const handleApproveAction = () => {
    handleApprove(true);
  };

  const handleRejectAction = () => {
    if (!feedbackValue.trim()) return;
    handleApprove(false, feedbackValue.trim());
    setFeedbackValue("");
  };

  // === 派生 renderBlocks: 折叠 messages 数组为 4 种类型化渲染块 ===
  // 目的: 让间距判断从"消息本身属性"升级为"前后块类型关系",更精准
  // 类型: user | reasoning | content | toolGroup
  const renderBlocks: RenderBlock[] = [];
  {
    let i = 0;
    while (i < messages.length) {
      const msg = messages[i];
      if (msg.role === "user") {
        renderBlocks.push({ type: "user", content: msg.content, id: `user-${i}` });
        i++;
      } else if (msg.role === "assistant") {
        if (msg.reasoning_content) {
          const hasContentNext = !!msg.content;
          const isLastFew = i >= messages.length - 2;
          renderBlocks.push({
            type: "reasoning",
            content: msg.reasoning_content,
            autoCollapse: isLastFew && !hasContentNext,
            id: `reasoning-${i}`,
          });
        }
        if (msg.content) {
          renderBlocks.push({ type: "content", content: msg.content, id: `content-${i}` });
        }
        i++;
      } else if (msg.role === "tool") {
        const tools: Message[] = [];
        const startIndex = i;   // 核心修复: 锁死这个 toolGroup 的起始索引, id 永远不变 (避免 React 卸载重挂载触发集体重入场)
        while (i < messages.length && messages[i].role === "tool") {
          tools.push(messages[i]);
          i++;
        }
        renderBlocks.push({ type: "toolGroup", tools, id: `toolGroup-${startIndex}` });
      }
    }
  }

  return (
    <div className="w-full h-screen flex flex-col bg-gradient-to-br from-slate-50 via-white to-slate-100 dark:from-zinc-950 dark:via-zinc-900 dark:to-zinc-950 text-slate-900 dark:text-zinc-50 font-sans">
      {/* Header Bar */}
      <ChatHeader
        docxPath={docxPath}
        isConnected={isConnected}
        tokenCount={tokenCount}
        hasDraftFiles={draftFiles.length > 0}
        showPreview={showPreview}
        sidebarOpen={sessionSidebarOpen}
        showWorkspace={showWorkspace}
        workspaceFileCount={workspaceFiles.length}
        currentSessionId={currentSessionId}
        streamMode={streamMode}
        editMode={editMode}
        canSwitchMode={!hasActiveConnection()}
        onEditModeChange={setEditMode}
        onToggleSidebar={() => {
          const nextOpen = !sessionSidebarOpen;
          setSessionSidebarOpen(nextOpen);
          // v2 fix: sidebar 打开时**懒加载**拉列表, 保证 UI 总是看到最新 session
          if (nextOpen) void refreshSessions();
        }}
        onTogglePreview={() => {
          // 主动点开时: 先拉最新数据再 toggle, 避免拿到陈旧列表
          if (!showPreview && currentSessionId) {
            void fetchDrafts(currentSessionId);
          }
          setShowPreview((v) => !v);
        }}
        onToggleWorkspace={() => {
          setShowWorkspace((v) => !v);
        }}
        onResetWorkspace={handleCreateSession}
        onToggleStreamMode={() => sendSetStreamMode(!streamMode)}
      />

      {/* 主区域: 横向 flex 父容器, 左侧 sidebar, 中 chat+input, 右侧 preview */}
      <div className="flex-1 w-full flex overflow-hidden">
        <SessionSidebar
          show={sessionSidebarOpen}
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSelect={handleSelectSession}
          onCreate={handleCreateSession}
          onDelete={handleDeleteSession}
          onClose={() => setSessionSidebarOpen(false)}
        />
        {/* 左侧: 聊天列表 + 输入框 */}
        <div className="flex-1 flex flex-col min-w-[400px]">
          {/* Main Chat Flow Container (修复 2: onScroll 绑定) */}
          <div
            className="flex-1 w-full overflow-y-auto"
            onScroll={handleScroll}
          >
          <div className="max-w-4xl w-full mx-auto py-6 space-y-6 px-4 md:px-8">
          {messages.length === 0 && !liveReasoning && !liveContent && (
            <div className="h-full flex flex-col items-center justify-center p-8 text-center text-slate-400 dark:text-zinc-500 select-none space-y-4">
              <div className="w-14 h-14 rounded-2xl bg-slate-100/80 dark:bg-zinc-800/80 shadow-sm flex items-center justify-center text-slate-400 dark:text-zinc-500">
                <Terminal className="w-6 h-6" />
              </div>
              <div className="max-w-md">
                <h3 className="text-sm font-semibold text-slate-700 dark:text-zinc-300">新建排版任务会话</h3>
                <p className="text-xs text-slate-400 dark:text-zinc-500 mt-2 leading-relaxed font-mono text-left">
                  请输入您的提问或排版需求，让 Agent 开始自主分析运行。<br /><br />
                  <strong>示例需求：</strong><br />
                  <span className="text-indigo-400 dark:text-indigo-400 text-[11px] block mt-1 bg-slate-100/60 dark:bg-zinc-800/60 p-3 rounded-xl leading-relaxed select-text">
                    把 <code>文档格式测试/cases/insert_text_001/docx/实验报告模板_v3_insert_text_001.docx</code> 中的"依据实验指导书"后插入"测试文本"，另存为 out/demo.docx，并对比原文档。
                  </span>
                </p>
              </div>
            </div>
          )}

          {/* === 4 种类型化消息块的渲染 (user / reasoning / content / toolGroup) === */}
          <ChatMessageBlocks
            blocks={renderBlocks}
            isGenerating={isGenerating}
            selectedToolId={selectedToolId}
            onSelectTool={setSelectedToolId}
            sessionId={currentSessionId}
          />

          {/* === 实时流式 AnimatedLivePanel === */}
          <AnimatedLivePanel reasoning={liveReasoning} content={liveContent} time={thinkTime} />

          {/* v3: 细致编辑模式 — 工具选用状态 */}
          {isSelectingTools && (
            <div className="flex items-center gap-3 px-4 py-3 bg-indigo-50/80 dark:bg-indigo-900/20 border border-indigo-200/50 dark:border-indigo-800/50 rounded-xl">
              <span className="w-2 h-2 rounded-full bg-indigo-500 animate-pulse" />
              <span className="text-xs font-mono text-indigo-600 dark:text-indigo-400">正在分析所需工具...</span>
            </div>
          )}
          {!isSelectingTools && selectedTools.length > 0 && editMode === "detail_edit" && (
            <div className="px-4 py-3 bg-slate-50/80 dark:bg-zinc-800/50 border border-slate-200/50 dark:border-zinc-700/50 rounded-xl space-y-2">
              <span className="text-[10px] font-mono font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-wider">当前工具集</span>
              <div className="flex flex-wrap gap-1.5">
                {selectedTools.map((t) => (
                  <span key={t} className="px-2 py-0.5 text-[10px] font-mono bg-white dark:bg-zinc-700 border border-slate-200 dark:border-zinc-600 rounded-md text-slate-600 dark:text-zinc-300">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* 旧的文档流内指示器已移除: 替换为 footer 上方悬浮胶囊(见下) */}

          {/* Inline Phase Checkpoint (Waiting Approval) — detail_edit 模式无审批 */}
          {editMode !== "detail_edit" && (
          <ApprovalCheckpoint
            isWaitingApproval={isWaitingApproval}
            approvalPhase={approvalPhase}
            isConnected={isConnected}
            feedbackValue={feedbackValue}
            onChangeFeedback={setFeedbackValue}
            onApprove={handleApproveAction}
            onReject={handleRejectAction}
          />
          )}

          <div ref={chatEndRef} />
          </div>
        </div>

        {/* Input Prompt Box area */}
        <ChatInput
          inputValue={inputValue}
          isConnected={isConnected}
          isWaitingApproval={isWaitingApproval}
          isGenerating={isGenerating}
          liveReasoning={liveReasoning}
          liveContent={liveContent}
          onChangeInput={setInputValue}
          onSubmit={handleSendPrompt}
        />
        </div>
        {/* 右侧: 预览侧栏 (内部 AnimatePresence 处理 0↔50% 动画) */}
        <PreviewPanel
          show={showPreview}
          files={draftFiles}
          activeFilename={activeFilename}
          onSelectFile={setActiveFilename}
          onClose={() => setShowPreview(false)}
          sessionId={currentSessionId}
          previewMode={previewMode}
          onPreviewModeChange={setPreviewMode}
          docxPreviewInfo={docxPreviewInfo}
        />
        {/* v2 (Phase 4): Workspace 文件工作区面板 (与 PreviewPanel 同 slot, 互斥) */}
        <WorkspacePanel
          show={showWorkspace}
          files={workspaceFiles}
          activeDocxName={activeDocxName}
          isUploading={isUploading}
          onSelectDocx={setActiveDocx}
          onUpload={async (file: File) => {
            let sessionId = currentSessionId;
            if (!sessionId) {
              // v3: 先通过 HTTP 创建正规 session (含 mode), 再上传
              const res = await fetch("/api/sessions", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ mode: editMode }),
              });
              if (!res.ok) throw new Error(`创建 session 失败: ${res.status}`);
              const data = await res.json();
              sessionId = data.session_id;
              setCurrentSessionId(sessionId);
            }
            setIsUploading(true);
            try {
              await uploadToWorkspace(sessionId!, file);
              void refreshSessions();
            } finally {
              setIsUploading(false);
            }
          }}
          onDelete={async (name: string) => {
            if (!currentSessionId) return;
            await deleteFromWorkspace(currentSessionId, name);
          }}
          onClose={() => setShowWorkspace(false)}
        />
      </div>
    </div>
  );
}
