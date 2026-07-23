"use client";

import { PanelLeft, FolderUp } from "lucide-react";

interface ChatHeaderProps {
  docxPath: string;
  isConnected: boolean;
  tokenCount: number;
  hasDraftFiles: boolean;
  showPreview: boolean;
  sidebarOpen: boolean;
  showWorkspace: boolean;
  workspaceFileCount: number;
  currentSessionId: string | null;
  streamMode: boolean;
  editMode?: "fill" | "detail_edit";  // v3
  canSwitchMode?: boolean;  // v3: 是否允许切换模式 (未连接时)
  onEditModeChange?: (mode: "fill" | "detail_edit") => void;  // v3
  onToggleSidebar: () => void;
  onTogglePreview: () => void;
  onToggleWorkspace: () => void;
  onResetWorkspace: () => void;
  onToggleStreamMode: () => void;
}

// === ChatHeader: 顶部 Header Bar ===
// 左侧: 会话管理按钮 + 标题 + docxPath 路径徽章
// 右侧: 连接状态点 + token 进度条 + 流式/非流式切换 + 草稿切换 + 文件 + 重置按钮
export default function ChatHeader({
  docxPath,
  isConnected,
  tokenCount,
  hasDraftFiles,
  showPreview,
  sidebarOpen,
  showWorkspace,
  workspaceFileCount,
  currentSessionId,
  streamMode,
  editMode,
  canSwitchMode,
  onEditModeChange,
  onToggleSidebar,
  onTogglePreview,
  onToggleWorkspace,
  onResetWorkspace,
  onToggleStreamMode,
}: ChatHeaderProps) {
  return (
    <header className="h-14 bg-white/70 dark:bg-zinc-900/70 backdrop-blur-md sticky top-0 z-50 px-6 flex items-center justify-between shrink-0">
      <div className="flex items-center gap-3">
        <button
          onClick={onToggleSidebar}
          className={`p-1.5 rounded-md border transition-colors cursor-pointer ${
            sidebarOpen
              ? "border-indigo-300 bg-indigo-50 dark:border-indigo-700 dark:bg-indigo-900/30"
              : "border-slate-200 dark:border-zinc-700 hover:bg-slate-50 dark:hover:bg-zinc-800"
          }`}
          aria-label="会话管理"
          title="会话管理"
        >
          <PanelLeft className="w-4 h-4 text-slate-600 dark:text-zinc-300" />
        </button>
        <span className="font-mono font-bold text-sm tracking-wider uppercase text-slate-800 dark:text-zinc-100">
          DOCX-Agent 交互工作台
        </span>
        {docxPath && (
          <span className="text-[10px] font-mono px-2 py-0.5 border border-slate-200 dark:border-zinc-700 bg-slate-50 dark:bg-zinc-800 text-slate-500 rounded truncate max-w-xs md:max-w-md">
            {docxPath}
          </span>
        )}
        {/* v3: 模式切换 (仅未连接时可用) */}
        {canSwitchMode && onEditModeChange && (
          <div className="inline-flex rounded-md border border-slate-200 dark:border-zinc-700 overflow-hidden ml-1">
            <button
              onClick={() => onEditModeChange("fill")}
              className={`px-2.5 py-1 text-[11px] font-mono transition-colors cursor-pointer ${
                editMode === "fill"
                  ? "bg-indigo-500 text-white"
                  : "bg-white dark:bg-zinc-800 text-slate-400 dark:text-zinc-500 hover:bg-slate-50 dark:hover:bg-zinc-700"
              }`}
            >
              填充
            </button>
            <button
              onClick={() => onEditModeChange("detail_edit")}
              className={`px-2.5 py-1 text-[11px] font-mono transition-colors border-l border-slate-200 dark:border-zinc-700 cursor-pointer ${
                editMode === "detail_edit"
                  ? "bg-indigo-500 text-white"
                  : "bg-white dark:bg-zinc-800 text-slate-400 dark:text-zinc-500 hover:bg-slate-50 dark:hover:bg-zinc-700"
              }`}
            >
              编辑
            </button>
          </div>
        )}
      </div>

      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <span className={`w-2.5 h-2.5 rounded-full ${isConnected ? "bg-emerald-500 animate-pulse" : "bg-red-500"}`} />
          <span className="text-xs font-mono text-slate-500">{isConnected ? "已连接" : "已断开"}</span>
        </div>

        {tokenCount > 0 && (
          <div className="flex items-center gap-2">
            <div className="w-24 h-1.5 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-300 ${
                  tokenCount > 150000 ? "bg-amber-500" : tokenCount > 100000 ? "bg-emerald-500" : "bg-indigo-500"
                }`}
                style={{ width: `${Math.min((tokenCount / 200000) * 100, 100)}%` }}
              />
            </div>
            <span className="text-[10px] font-mono text-slate-400">
              {tokenCount > 1000 ? `${(tokenCount / 1000).toFixed(0)}k` : tokenCount}
            </span>
          </div>
        )}

        {/* v2: 流式 / 非流式 切换按钮 (SenseNova SSE stall 修复) */}
        <button
          onClick={onToggleStreamMode}
          title={streamMode
            ? "当前: 流式 (实时显示思考过程). 点击切换为非流式 — 适合商汤等 SSE 不稳定的 provider. 也是下一次新建会话的默认模式"
            : "当前: 非流式 (等待完整响应). 点击切换为流式. 也是下一次新建会话的默认模式"}
          className={`flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-mono font-semibold border rounded transition-colors cursor-pointer ${
            streamMode
              ? "border-indigo-200 dark:border-indigo-800 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30"
              : "border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400 hover:bg-amber-100 dark:hover:bg-amber-900/30"
          }`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${streamMode ? "bg-indigo-500" : "bg-amber-500"}`} />
          {streamMode ? "流式" : "非流式"}
        </button>

        {/* v2 (Phase 4 + 修复): 文件工作区切换按钮 — 始终可见
            无 session 时点按钮会先触发 onToggleWorkspace 内部逻辑建空 session
            (见 page.tsx onToggleWorkspace handler) */}
        <button
          onClick={onToggleWorkspace}
          className={`flex items-center gap-1.5 px-3 py-1 text-xs font-semibold border rounded transition-colors cursor-pointer ${
            showWorkspace
              ? "border-indigo-300 bg-indigo-50 dark:border-indigo-700 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400"
              : "border-indigo-200 dark:border-indigo-800 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400"
          }`}
          title="上传/管理文件"
        >
          <FolderUp className="w-3.5 h-3.5" />
          文件 {workspaceFileCount > 0 && `(${workspaceFileCount})`}
        </button>

        {hasDraftFiles && (
          <button
            onClick={onTogglePreview}
            className="px-3 py-1 text-xs font-semibold border border-indigo-200 dark:border-indigo-800 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 rounded transition-colors cursor-pointer text-indigo-600 dark:text-indigo-400"
          >
            {showPreview ? "隐藏草稿" : "查看草稿"}
          </button>
        )}

        <button
          onClick={onResetWorkspace}
          className="px-3 py-1 text-xs font-semibold border border-slate-200 dark:border-zinc-700 hover:bg-slate-50 dark:hover:bg-zinc-800 rounded transition-colors cursor-pointer text-slate-600 dark:text-zinc-300"
        >
          新建会话
        </button>
      </div>
    </header>
  );
}
