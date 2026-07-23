# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DOCX Agent — AI-driven Word document editor using LLM tool calling + direct OpenXML manipulation. No `python-docx` dependency; operates directly on ZIP-wrapped XML.

## Build Commands

Primary development surface is the FastAPI + WebSocket backend paired with the Next.js frontend. Run both during development.

```powershell
# Python backend (primary)
pip install -r requirements.txt
python src/server.py                    # API + WebSocket server on http://127.0.0.1:8000

# Frontend (Next.js, primary)
cd frontend
npm install
npm run dev                             # Development server on http://localhost:3000
```

Legacy entry point (kept for backward compatibility, not the primary development path):

```powershell
python src/docx_agent_demo.py           # Single-shot CLI demo (writes to global logs/)
```

## Architecture

### Three-Stage State Machine

LLM uses only tools permitted for the current stage:

```
样式审核 (Style Review) → Markdown 草稿 (MD Draft) → Word 写入编译 (Word Editing)
```

| Stage | Tools |
|-------|-------|
| Style Review | `analyze_docx_style_samples`, `bind_styles_to_roles`, `read_docx_structure`, `ls` |
| MD Draft | `write_markdown_draft`, `read_markdown_draft`, `parse_markdown_draft`, `ls`, `read`, `analyze_image_content`, `generate_image`, `render_diagram` |
| Word Editing | `read_docx_structure`, `write_markdown_draft`, `read_markdown_draft`, `parse_markdown_draft`, `markdown_to_word`, `diff_docx`, `ls`, `read`, `analyze_image_content`, `generate_image`, `render_diagram` |

Authoritative source of truth: `*_TOOL_NAMES` sets in `src/prompts.py` and `tool_schemas_for_state()` filtering.

### Detail Edit Mode (detail_edit)

Second workflow mode for fine-grained editing of completed documents. Parallel to the three-stage fill mode.

```
start(mode="detail_edit") → tool selector agent → free editing loop → done
```

- **Tool selector** (`src/tool_selector.py`): Internal LLM call (blocking, no tools) that picks a tool subset from the full catalog based on user request + document structure.
- **Base tools (6, always available)**: `read_docx_structure`, `find_text`, `ls`, `read`, `diff_docx`, `request_more_tools`. Defined in `DETAIL_EDIT_BASE_TOOLS` in `src/prompts.py`.
- **Selected tools**: Write/modify atomic tools chosen by the selector. Filtered via `tool_schemas_for_detail_edit()` in `src/prompts.py`.
- **Tool expansion**: Editing agent calls `request_more_tools(reason)` → selector re-evaluates → only-add-never-remove. Handled in `Agent._handle_request_more_tools()` in `src/agent.py`.
- **No approval**: Free editing loop, ends when LLM produces content without tool calls.
- **WS protocol**: `start` frame accepts `mode` field; `session_created`/`history` frames include `mode`; new events `tool_selection_start`/`tool_selection_end`.
- **Persistence**: `mode` in metadata.json, `selected_tools` + `doc_structure_cache` in workflow.json.

### Core Systems

**Tool Registry** (`src/docx_tools/registry.py`): All 35 tools registered here in two parallel structures (`TOOLS` dict for dispatch, `TOOLS_SCHEMA` list for LLM-facing schemas). Tools grouped into `basic_tools/`, `md_tools/`, and `docx_tools/`.

**Adding a new LLM-visible tool — four-place registration** (missing any one produces a different failure mode):

1. `src/docx_tools/registry.py` — append to **both** `TOOLS` dict and `TOOLS_SCHEMA` list. Missing → LLM can't see / can't dispatch.
2. `src/prompts.py` — add the tool name to the right `*_TOOL_NAMES` set for each stage that should expose it, and (when behavior nudging is needed) add a clause to the matching `state_rule`. Missing → tool is dispatchable but invisible to the LLM in that stage.
3. `src/agent.py` — add the tool name to `SESSION_TOOLS` if its Python signature takes `session_id`. Missing → tool is visible and selected, but the dispatcher won't inject `session_id`, so the call dies with `missing 1 required positional argument: 'session_id'`.
4. Tests — at minimum a unit test of the tool function in `tests/test_<tool>.py`. Recommended: a real-Kroki / real-API e2e probe to verify the integration layer beyond the in-process fixture.

The `SESSION_TOOLS` set in `src/agent.py:24-63` is the hidden fourth registry — its existence is easy to miss because it doesn't appear in any module-level `__all__` or `TOOLS_SCHEMA`. A typo or missing entry here only surfaces when a real WebSocket session calls the tool; unit tests with fixture-injected `session_id` will silently pass.

**Markdown Compiler Pipeline** (`src/docx_compiler/`):
```
Markdown text → markdown-it-py → MarkdownBlock AST → lower → Layout IR → render → OpenXML lxml tree → .docx
```
Key files: `ir.py` (ParagraphIR, TableIR, ImageIR, etc.), `markdown_parser.py` (AST), `lower.py` (AST→IR), `render.py` (IR→OpenXML).

**LLM Adapter** (`src/llm_adapter.py`): Abstract layer supporting DeepSeek (with `thinking`), SenseNova (with `reasoning_effort`), and OpenAI-compatible APIs. Model selection via `config.json` or `LLM_PROVIDER`/`DEEPSEEK_API_KEY` env vars.

**Server** (`src/server.py`): FastAPI + WebSocket for browser-based interactive editing with real-time state machine visualization. **Each WebSocket session logs to `out/sessions/<id>/logs/<session>.log` inside its own sandbox** — deleting the session purges its logs. The legacy `docx_agent_demo.py` CLI still writes to a global `logs/docx_agent_<timestamp>.log` (history kept for backward compatibility; not the primary path).

### Key Constraints

- Table operations: `table_index` counts all `//w:tbl` elements including nested tables; verify with `read_docx_structure` (depth, parent coords, `direct_text`) before operating.
- Paragraph operations: must pass both `paragraph_index` and `anchor_text` to prevent misalignment.
- Image analysis: always use `analyze_image_content` for visual content; never infer from filenames.
- Image generation (写实风格): `generate_image` 工具内部封装了 sub-agent 审核循环 (商汤 vision 模型当审核员), 自动迭代重生直到图片质量合格。子 agent 的 reasoning 完整保留在 session log, 主 LLM 只看到最终路径。Schema 用 size enum (11 种 2K 尺寸) 约束 LLM 避免 400 错误;`max_iterations` 上限 5 防止 WS 假死。**仅用于装饰性/写实图;有逻辑结构的图请用 `render_diagram`。**
- Diagram rendering (结构化图): `render_diagram` 让 LLM 直接写 Graphviz DOT 或 Mermaid 源码,通过 [kroki.io](https://kroki.io) 在线渲染成 PNG,落到 `media/<filename>.png`。流程图、状态机、架构图、组织结构图、时序图、类图等节点-边模型应优先用它,不要用 generate_image(文生图对结构化图节点错位、文字模糊)。**协议陷阱**:Kroki 要完整 zlib 包装(`78 ??` 头 + adler32 尾),不要剥头尾——`test_kroki_encode_keeps_zlib_header_and_checksum` 钉死了这条不变量。
- Large files: use `read` with `offset`/`limit` (max ~500 lines per call).
- Run preservation: when inserting text, prefer preserving original run formatting.

## Directory Structure

```
src/
├── server.py             # FastAPI + WebSocket server (primary entry)
├── docx_agent_demo.py    # Legacy single-shot CLI (kept for backward compat)
├── llm_adapter.py        # LLM provider abstraction (re-export shim)
├── config.json           # API keys and provider config (gitignored)
├── prompts.py            # 阶段名常量 + *_TOOL_NAMES 集合 + state_rule
├── basic_tools/          # ls, read, analyze_image_content, generate_image, render_diagram
│   ├── _media.py        # 共享: 图片下载/编码/消息构造
│   └── ...
├── agents/               # 内部 sub-agent 编排层 (主 agent 看不到)
│   └── image_refiner.py # 生成图审核-重生 sub-agent
├── md_tools/             # Markdown draft and markdown_to_word compiler
├── docx_tools/           # 36+ tools + registry
│   ├── registry.py      # TOOLS / TOOLS_SCHEMA / call_tool
│   ├── common.py        # Shared XML/ZIP utilities (incl. word/media/ injection)
│   ├── diff_docx.py     # XML/text diff
│   └── *.py             # Individual tool implementations
├── docx_compiler/        # Markdown → DOCX compiler
│   ├── ir.py            # Intermediate representation types
│   ├── markdown_parser.py
│   ├── lower.py         # AST → IR lowering + diagnostics
│   ├── render.py        # IR → OpenXML rendering
│   ├── optimizer.py     # XML optimization
│   └── table_ops.py     # Table operations
├── llm_adapter/          # LLM provider 抽象
│   ├── constants.py     # SENSENOVA_U1_VALID_SIZES 等模型枚举
│   ├── provider.py      # LLMClient 主类 (含 create_image_generation)
│   └── ...
└── workspace/            # 沙箱根、路径解析、配额防御
    └── guard.py         # workspace_dir, resolve_workspace_path
frontend/
├── app/                  # Next.js app router pages
├── AGENTS.md             # Next.js breaking changes notice (important!)
└── ...
out/
├── sessions/<id>/        # Web session 沙箱 (workspace + logs + drafts + uploads)
└── drafts/              # Legacy: demo CLI 的草稿输出
文档格式测试/               # Test cases with before/after DOCX pairs
tests/                   # pytest suite (287 passed @ ~24s)
logs/                    # Legacy: demo CLI 的全局日志 (Web sessions log inside out/sessions/<id>/logs/)
```

## Frontend Note

This project uses a **non-standard Next.js version** with breaking API changes. Before modifying frontend code, read `frontend/AGENTS.md` and the Next.js guides in `node_modules/next/dist/docs/`.