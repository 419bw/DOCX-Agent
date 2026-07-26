# DOCX Agent

AI 驱动的 Word 文档编辑 Agent，基于 LLM 工具调用 + 直接 OpenXML 操作实现精细化的 `.docx` 编辑。无需 `python-docx`，直接操控底层 XML 和 ZIP 结构。

后端 FastAPI + 前端 Next.js 双服务架构，**会话状态全部持久化到磁盘文件系统**——刷新页面、切 session、跨设备同步都不丢。

## 核心特性

- **三阶段状态机** — 样式审核 → Markdown 草稿 → Word 写入，每阶段 LLM 只能用当前允许的工具集
- **直接 XML 编辑** — 基于 `lxml` + `zipfile` 直接读写 `word/document.xml`，精确操控段落、run、表格、样式
- **Markdown 编译器** — `markdown → AST → IR → OpenXML` 多阶段编译管线，支持标题、段落、列表、表格、代码块、公式、图片
- **图片插入** — 本地图片嵌入，自动处理 DrawingML XML、关系链、Content Type 注册
- **多模态识图** — 适配多模态视觉模型（SenseNova）分析图表与截图
- **样式感知** — 分析模板格式后由 AI 自定义各角色的字体、字号、对齐等参数，编辑时保持统一风格
- **变更验证** — 编辑后自动 diff 对比原文档
- **会话持久化（v2）** — 每个 session 一个目录 `out/sessions/<id>/`，含 3 个 JSON + 子目录；前端删 IndexedDB，后端是 source of truth
- **切 session 不掉线（v2）** — WS resume 重建上下文，approval 按钮可继续点

## 三阶段状态机

```
样式审核 (Style Review) → Markdown 草稿 (MD Draft) → Word 写入编译 (Word Editing)
```

| 阶段 | 职责 | 可用工具（节选） |
|------|------|---------|
| **样式审核** | 分析模板格式 + AI 自定义格式参数 | `analyze_docx_style_samples`, `define_style_profile`, `read_docx_structure`, `ls` |
| **Markdown 草稿** | 按文档区域生成结构化 Markdown 内容 | `write_markdown_draft`, `read_markdown_draft`, `parse_markdown_draft`, `ls`, `read`, `analyze_image_content` |
| **Word 写入** | 编译 Markdown 写入 DOCX，diff 验证 | `read_docx_structure`, `write_markdown_draft`, `read_markdown_draft`, `parse_markdown_draft`, `markdown_to_word`, `diff_docx`, `ls`, `read`, `analyze_image_content` |

## 细致编辑模式 (detail_edit)

与三阶段填充模式并列的第二种工作模式，用于对**已完成文档**做精细化编辑。

```
start(mode="detail_edit") → 工具选用 agent 选工具子集 → 自由编辑循环 → done
```

- **工具选用 agent**：内部 LLM 调用（阻塞、无 tools），根据用户请求 + 文档结构从 36+ 工具中选出最合适的子集
- **基础工具（6 个，始终可用）**：`read_docx_structure`, `find_text`, `ls`, `read`, `diff_docx`, `request_more_tools`
- **选用工具**：写入/修改类原子工具（`replace_text`, `insert_text_at`, `set_text_format` 等），由选用 agent 按需挑选
- **工具扩充**：编辑 agent 调 `request_more_tools(reason)` → 选用 agent 重新评估 → 只增不减
- **无审批**：自由编辑循环，LLM 认为完成即结束

前端在输入框上方提供"填充模式 / 编辑模式"切换，仅未连接时可切换。

## v2 架构：HTTP 控制面 + WS 数据面

后端是 **session 状态的 source of truth**，前端只展示不缓存。

### 持久化结构

每个 session 一个目录，白盒可调试（vs pickle 黑盒）：

```
out/sessions/session-20260607-143052/
├── metadata.json           # 会话元数据 (id, title, createdAt, docxPath, workflow_state, pending_approval)
├── messages.json           # 完整 LLM 消息历史 (MessageManager._entries)
├── workflow.json           # 状态机 (workflow_state, stage_called_tools, draft_files_written, _round_index)
├── logs/
│   └── agent_<timestamp>.log
├── drafts/                 # write_markdown_draft 写到这里 (沙箱化)
│   ├── cover.md
│   └── section1.md
├── style_profiles/         # analyze_docx_style_samples 写到这里 (沙箱化)
│   └── 模板_v3_<timestamp>.json
└── uploads/                # 用户上传的 docx (沙箱化)
    └── 实验报告.docx
```

### HTTP / WS 边界

| 方向 | 通道 | 用途 |
|------|------|------|
| 前→后 | `GET /api/sessions` | 列表（sidebar 打开时拉一次） |
| 前→后 | `GET /api/sessions/{id}` | 单个 session 快照（可选） |
| 前→后 | `DELETE /api/sessions/{id}` | 级联删整个 session 目录 |
| 前→后 | `WS /api/ws/agent` `start` | 新建会话（后端生成 session_id） |
| 前→后 | `WS /api/ws/agent` `resume` | 恢复会话（从磁盘反序列化） |
| 后→前 | `WS` `session_created` | start 成功 |
| 后→前 | `WS` `history` | resume 成功（含 messages + approvalPhase + isWaitingApproval） |
| 后→前 | `WS` 其它事件 | `round_start` / `reasoning` / `content` / `tool_start` / `tool_end` / `wait_approval` / `done` / `error` / `heartbeat` |

> **关键设计**：WS 协议**不**承载列表/删除查询（避免 in-band 消息 race 与协议分裂）。
> 
> 注：**v2.1 移除** `POST /api/upload`（前端无上传入口，避开 Next.js dev server multipart rewrites 不稳的风险）— 当前 3 个 HTTP endpoint 全是 JSON。

### 3 个避坑（v2 关键设计）

1. **session_id 永不暴露给 LLM** — 工具 `tools_schema` 保持纯洁，agent dispatcher **反射调用前隐式注入** `self.session_id`，并**用 self 覆盖 LLM 传的值**（防幻觉/越权）
2. **写盘异步锁** — Agent 类持 `asyncio.Lock()`，同一 session 同一时刻只有一个写盘线程（避免 `tool_start`/`tool_end` 毫秒级间隔时的文件写花）
3. **HTTP/WS 边界** — 列表/删除走 HTTP（无状态查询），WS 只 `start`/`resume` + 事件流（避免协议分裂）

### 6 Checkpoint 写盘时机

Fire-and-forget 后台任务，**不阻塞 stream**：

1. `round_start` 后（用户发消息后，message 已发）
2. `tool_start` 时（工具"running"状态）
3. `tool_end` 时（工具"success/error"状态）
4. STYLE_REVIEW `wait_approval` yield 前（**关键**：挂起前必须落盘）
5. MD_DRAFT `wait_approval` yield 前（**关键**：挂起前必须落盘）
6. `done` yield 前（完结）

`Agent.save_to_disk()` 写 3 个 JSON（`metadata` / `messages` / `workflow`）只 ~10ms，**`start` 后立即同步写盘**让 `/api/sessions` 立即能看到新 session。

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+
- 后端 Python 依赖（完整见 `requirements.txt`）：
  - **核心**：`openai` / `lxml` / `markdown-it-py` / `Pillow`
  - **v2 服务**：`fastapi` / `uvicorn` / `websockets` / `pydantic`

### 配置

复制并编辑 `src/config.json`（gitignored）。**v2 schema**（commit `8d03835` refactor/llm-adapter-registry 落地后，新接入 LLM provider 只需在 `providers` 块加 4 行，无需改任何 .py 代码）：

```json
{
  "provider": "通用接口",
  "provider_profiles": {
    "openai_compatible": {
      "capabilities": ["chat", "tools"],
      "reasoning_field": "delta.reasoning_content",
      "forward_tool_choice": false,
      "quirks": [],
      "extra_body_template": null,
      "top_level_kwargs": {}
    }
  },
  "providers": {
    "通用接口": {
      "profile": "openai_compatible",
      "api_key": "sk-YOUR_KEY",
      "base_url": "https://your-endpoint.example.com/v1",
      "model": "your-model-name"
    }
  }
}
```

完整示例（4 种典型定制形态）见 `src/config.example.json`。

也支持环境变量覆盖（`src/llm_adapter/provider.py` 实现，优先级：env > config）：

```powershell
# 选 provider
$env:LLM_PROVIDER="通用接口"

# API key (任选其一, LLM_API_KEY 通用 fallback)
$env:DEEPSEEK_API_KEY="your-key"   # 或 SENSENOVA_API_KEY / AGNES_API_KEY / OPENAI_API_KEY
$env:LLM_API_KEY="your-key"        # 通用

# Endpoint / 模型
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:LLM_MODEL="deepseek-chat"      # 或 OPENAI_MODEL / AGNES_MODEL

# Thinking / reasoning
$env:DOCX_AGENT_THINKING="enabled"
$env:SENSENOVA_REASONING_EFFORT="high"
```

### 运行（v2 必起双服务）

```powershell
# Terminal 1: 后端 FastAPI :8000 (HTTP API + WebSocket)
pip install -r requirements.txt
python src/server.py

# Terminal 2: 前端 Next.js :3000
cd frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:3000` 即可。

> **前端 fetch `/api/*` 通过 `next.config.ts` rewrites 代理到后端 :8000** ——前端代码不感知后端地址，无 CORS 问题。改了 `next.config.ts` 必须**重启 `npm run dev`**（配置只启动时读一次）。
>
> **WebSocket** (`ws://127.0.0.1:8000/api/ws/agent`) 走绝对地址——Next.js dev server 不支持 WS 代理。

### CLI Demo

不想起前端也能跑：

```powershell
python src/docx_agent_demo.py
```

## 测试

v2 完整测试套 **138/138** 通过（`tests/`，13 个 file）：

| 测试 | 范围 | 通过 |
|------|------|------|
| `test_step1.py` | Agent save/load + 异步锁 + 6 Checkpoint | 5/5 |
| `test_step2.py` | WS `start`/`resume` 协议 + `session_created`/`history` 响应（6 个分支）+ `isolate_sessions_root` fixture 隔离 SESSIONS_ROOT 跨 file 污染 | 6/6 |
| `test_step3.py` | HTTP 控制面 3 endpoint (sessions list/get/delete) + 旧 draft API 删除 | 8/8 |
| `test_step4.py` | dispatcher 隐式注入 session_id + 6 工具沙箱化 + 路径守卫 | 10/10 |
| `test_step5.py` | 前端删 IndexedDB + HTTP fetch + next.config.ts rewrites（10 + 5 fixup，含 `test_page_tsx_startup_does_not_auto_resume` v2.2 新加） | 15/15 |
| `test_agent_blocking.py` | 非流式路径（`create_chat_completion_blocking`，商汤 SSE stall 修复） | 7/7 |
| `test_stream_incomplete_retry.py` | B 方案流不完整重试（`finish_reason=None` 防御） | 4/4 |
| `test_llm_adapter_step1_parity.py` | llm_adapter 请求构建 parity（profile 化前/后等价） | 13/13 |
| `test_llm_adapter_step2_capability.py` | capability 接口（`has_capability`） | 14/14 |
| `test_llm_adapter_step3_reasoning.py` | `extract_reasoning` 抽象（`reasoning_field` 路径） | 14/14 |
| `test_llm_adapter_step4_quirks.py` | quirk 注册与 `apply_quirk` | 12/12 |
| `test_llm_adapter_step5_profiles.py` | profile 配置化 + 数据驱动注入 | 22/22 |
| `test_llm_adapter_step6_cleanup.py` | 清理遗留 + `DeprecationWarning` | 8/8 |

```bash
# 推荐: 跑全部
python -m pytest tests/

# 单跑 step 文件
python tests/test_step1.py
python tests/test_step2.py
python tests/test_step3.py
python tests/test_step4.py
python tests/test_step5.py
```

## 支持的 LLM

v2 设计：**profile + 任意 OpenAI 兼容 provider**，新接入 4 行（`api_key` / `base_url` / `model` / 可选 `profile`）即可，无需改任何 `.py` 代码。详见 [配置](#配置) 段的 v2 schema 示例与 `src/config.example.json`。

内置 provider 别名（`src/llm_adapter/provider.py` 通过 env var 切换，不需要在 `config.json` 里建块）：

| 别名 | 适用 | env var |
|------|------|---------|
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `LLM_MODEL` |
| SenseNova (商汤) | `https://token.sensenova.cn/v1` | `SENSENOVA_API_KEY` / `SENSENOVA_BASE_URL` / `SENSENOVA_REASONING_EFFORT` |
| AGNES | AGNES 网关 | `AGNES_API_KEY` / `AGNES_BASE_URL` / `AGNES_MODEL` |
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` |

通用 fallback：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `DOCX_AGENT_THINKING` / `LLM_REASONING_EFFORT`。

## 工具总览（v2 沙箱化）

LLM 看到的 30+ 个工具，**全部**接受业务参数（不感知 `session_id`）：

### 基础工具 (`basic_tools/`)

| 工具 | 说明 |
|------|------|
| `ls` | 列出目录内容 |
| `read` | 读取文本/代码/Markdown，支持 offset/limit 分页 |
| `analyze_image_content` | 多模态图像识别（SenseNova） |

### Markdown 工具 (`md_tools/`) — 沙箱化

工具的 `tools_schema` **不**含 `session_id` 字段，文件写到 `out/sessions/<id>/drafts/`：

| 工具 | 写入路径 | 读取路径 |
|------|----------|----------|
| `write_markdown_draft` | `session_dir/drafts/` | — |
| `read_markdown_draft` | — | `session_dir/drafts/` |
| `parse_markdown_draft` | — | `session_dir/drafts/` |
| `markdown_to_word` | — | `session_dir/drafts/`（内部分发到 apply_*） |
| `apply_markdown_ir_to_table_cell` | — | `session_dir/drafts/` |
| `apply_markdown_ir_after_paragraph` | — | `session_dir/drafts/`（内部 helper） |

### DOCX 编辑工具 (`docx_tools/`) — 沙箱化

| 工具 | 说明 |
|------|------|
| `analyze_docx_style_samples` | 提取文档格式样本 → 写到 `session_dir/style_profiles/` |
| `read_docx_structure` | 读取段落文本、表格结构、位置坐标 |
| `define_style_profile` | AI 自定义各角色格式参数（字体/字号/加粗/颜色/对齐/底纹），写入 style_profile.json |
| `find_text` | 搜索文本，返回段落索引和字符偏移 |
| `insert_text_at` | 在锚点偏移处插入文本 |
| `insert_text_in_table_cell` | 在表格单元格内插入文本 |
| `replace_text` | 替换文档文本（支持跨 run 匹配） |
| `delete_text` | 删除指定文本 |
| `insert_paragraph_after` | 在锚点段落后插入新段落 |
| `insert_image_after_paragraph` | 在段落后插入本地图片 |
| `set_text_format` | 设置字符格式 |
| `set_paragraph_indent` | 设置段落缩进 |
| `diff_docx` | 对比两个 .docx 文件 |
| `unzip_docx` | 解压 .docx |
| `replace_text_like_sample` | 按样式样本替换文本 |
| `replace_table_cell_like_sample` | 按样式样本替换单元格文本 |
| `replace_table_cell_text` | 替换表格单元格文本 |
| `insert_paragraph_after_like_sample` | 按样式样本插入段落 |
| `insert_table_row_after` | 在指定行后插入表格行 |
| `insert_table_after_paragraph` | 在段落后插入表格 |
| `insert_table_in_cell` | 在单元格内插入嵌套表格 |
| `insert_table_column_after` | 在指定列后插入列 |
| `merge_table_cells_horizontal` | 水平合并单元格 |
| `clear_table_cell` | 清空单元格内容 |
| `delete_table_row` | 删除表格行 |

## 编译器架构

```
Markdown 文本
    ↓ markdown-it-py 分词
MarkdownBlock AST（HeadingBlock, ParagraphBlock, TableBlock, ImageBlock...）
    ↓ normalize_block_support + diagnostics
    ↓ lower
Layout IR（ParagraphIR, TableIR, CodeBlockIR, FormulaIR, ImageIR...）
    ↓ render
OpenXML lxml 元素树
    ↓ write_document_xml
输出 .docx
```

## 上下文管理

`MessageManager` 自动管理消息累积和去重：

- 内部使用追加模式，两遍倒序扫描实现自动去重
- 去重范围：`write_markdown_draft` 和 `read_markdown_draft`（同一文件的多次写入只保留最新）
- 配对删除：tool call 和其 tool result 一起删除
- Token 追踪：累计 `prompt_tokens`，前端 Header 实时显示

## 目录结构

```
src/
├── docx_agent_demo.py         # CLI 入口
├── server.py                  # FastAPI + WebSocket + HTTP 控制面
├── llm_adapter/               # LLM 客户端抽象（DeepSeek / SenseNova / OpenAI 兼容）
│   ├── __init__.py            # 导出 LLMClientAdapter
│   ├── provider.py            # LLMClient 主类（流式 + 非流式 + capability 接口）
│   ├── config.py              # profile 配置加载
│   ├── registry.py            # PROFILES + pick_capable_adapter
│   ├── request_builder.py     # build_request_kwargs (Step 5 数据驱动注入)
│   ├── response_parser.py     # extract_reasoning (Step 3 reasoning_field 抽象)
│   └── quirks.py              # QuirkAction / QuirkDirective / apply_quirk (Step 4)
├── context_manager.py         # 消息管理（去重 + token 追踪）
├── prompts.py                 # 状态名常量 + SYSTEM_PROMPT + state_prompt (Step A 拆出)
├── state_machine.py           # WorkflowTransitions 静态评估 + TransitionDirective (Step C 拆出)
├── session_persistence.py     # SessionPersistence 类 (Step B 拆出, weakref 避免循环引用)
├── agent.py                   # Agent 核心类（async generator）+ 6 Checkpoint 委托
├── config.json                # API 配置（gitignored）
├── basic_tools/               # 基础工具
│   ├── ls.py / read.py / analyze_image_content.py
├── md_tools/                  # Markdown 草稿与编译器
│   ├── common.py              # draft_path() 路径守卫 (拒绝 .. / /etc/)
│   ├── write_markdown_draft.py
│   ├── read_markdown_draft.py
│   ├── parse_markdown_draft.py
│   ├── markdown_to_word.py
│   └── apply_markdown_ir_*.py
├── docx_tools/                # DOCX 编辑工具（30+ 个）
│   ├── registry.py            # TOOLS_SCHEMA + call_tool (反射调用 + kwargs 展开)
│   ├── common.py
│   ├── analyze_docx_style_samples.py  # profile 写到 session_dir/style_profiles/
│   ├── define_style_profile.py  # AI 自定义格式参数
│   ├── bind_styles_to_roles.py  # (已废弃, 保留兼容)
│   ├── read_docx_structure.py
│   ├── find_text.py / replace_text.py / ...
│   └── diff_docx.py
├── docx_compiler/             # Markdown → DOCX 编译器
│   ├── ir.py                  # 中间表示
│   ├── markdown_parser.py     # Markdown AST
│   ├── lower.py               # AST → IR
│   ├── render.py              # IR → OpenXML
│   ├── optimizer.py
│   ├── diagnostics.py
│   └── table_ops.py
├── 文档格式测试/               # 测试用例 (before/after DOCX 对)
└── out/                       # 输出（gitignored）
    └── sessions/              # v2: 每个 session 一个目录
        └── session-YYYYMMDD-HHMMSS/
            ├── metadata.json / messages.json / workflow.json
            ├── logs/ / drafts/ / style_profiles/              # uploads/ 在 v2.1 删 (前端无入口)

frontend/                     # Next.js (非标准, 看 frontend/AGENTS.md)
├── app/
│   ├── page.tsx              # 主交互页面 (~403 行, Step 6-9 拆出 ChatHeader/ChatInput/ChatMessageBlocks/ApprovalCheckpoint)
│   ├── layout.tsx
│   ├── globals.css
│   └── test-stream/          # 早期流式 demo (当前主流程不走)
├── components/                # 9 个 UI 组件
│   ├── session-sidebar.tsx   # 左侧 session 列表
│   ├── chat-header.tsx       # Header bar (流式/非流式 toggle + sidebar 开关)
│   ├── chat-input.tsx        # 底部 prompt 输入
│   ├── chat-message-blocks.tsx # 4 种类型化消息块 (user/reasoning/content/toolGroup)
│   ├── approval-checkpoint.tsx  # wait_approval 内联面板
│   ├── preview-panel.tsx     # 右侧草稿预览 (md_draft 阶段自动展开)
│   ├── animated-live-panel.tsx  # 实时流式动画
│   ├── markdown-renderer.tsx # Markdown 渲染
│   └── reasoning-panel.tsx   # 推理内容折叠展示
├── hooks/
│   └── use-agent-session.ts  # 核心 hook (Step 4 提取): WS 生命周期 + 实时流 + approval state
├── lib/
│   ├── session-types.ts      # 共享 SessionMeta type (v2: 删 lib/sessions.ts IndexedDB)
│   ├── message-types.ts      # 共享 Message / RenderBlock type (Step 1 提取)
│   └── draft-types.ts        # 共享 DraftFile type
└── next.config.ts             # rewrites 代理 /api/* -> 后端 :8000

tests/                        # 138/138 测试通过 (Step 2 fixture 隔离 SESSIONS_ROOT 跨 file 污染)
├── test_step1.py             # save/load + 锁 + Checkpoint
├── test_step2.py             # WS start/resume 协议
├── test_step3.py             # HTTP 控制面
├── test_step4.py             # dispatcher 注入 session_id
└── test_step5.py             # 前端静态契约 + next.config.ts rewrites
```
