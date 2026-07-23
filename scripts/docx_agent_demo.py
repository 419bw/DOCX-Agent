import json
import os
from datetime import datetime
from pathlib import Path

from openai import APIConnectionError, APIError, APITimeoutError

from llm_adapter import LLMClientAdapter
from docx_tools import TOOLS_SCHEMA, call_tool, render_tools_prompt

STYLE_REVIEW = "style_review"
MD_DRAFT = "md_draft"
WORD_EDITING = "word_editing"
REVIEW_TOOL_NAMES = {"analyze_docx_style_samples", "read_docx_structure", "ls"}
MD_DRAFT_TOOL_NAMES = {
    "write_markdown_draft",
    "read_markdown_draft",
    "parse_markdown_draft",
    "ls",
    "read",
    "analyze_image_content",
    "generate_image",
}
WORD_EDITING_TOOL_NAMES = {
    "read_docx_structure",
    "write_markdown_draft",
    "read_markdown_draft",
    "parse_markdown_draft",
    "markdown_to_word",
    "diff_docx",
    "ls",
    "read",
    "analyze_image_content",
    "generate_image",
}



def create_log_file() -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"docx_agent_{timestamp}.log"


def append_log(log_path: Path, title: str, data=None) -> None:
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


def tool_status(result: str) -> str:
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return "完成"
    status = parsed.get("status")
    if status:
        return str(status)
    return "完成"


def read_user_input(prompt: str) -> str | None:
    user_input = input(prompt).strip()
    if user_input.lower() in {"quit", "exit"}:
        return None
    return user_input


def read_yes_no(prompt: str) -> bool | None:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"quit", "exit"}:
            return None
        if answer == "y":
            return True
        if answer == "n":
            return False
        print("请输入 Y 或 N；输入 quit/exit 结束。")


def tool_schemas_for_state(state: str):
    if state == STYLE_REVIEW:
        allowed = REVIEW_TOOL_NAMES
    elif state == MD_DRAFT:
        allowed = MD_DRAFT_TOOL_NAMES
    else:
        allowed = WORD_EDITING_TOOL_NAMES
    return [schema for schema in TOOLS_SCHEMA if schema["function"]["name"] in allowed]


def tool_names(tool_schemas) -> set[str]:
    return {schema["function"]["name"] for schema in tool_schemas}


def state_prompt(state: str, available_tool_schemas) -> str:
    if state == STYLE_REVIEW:
        state_rule = """
当前状态：样式审核。
你的任务：仅对模板文档进行只读分析，提取格式特征与文档结构。
规则：
1. 你现在只能做样式和结构分析，不能编辑文档。
2. 请优先调用 analyze_docx_style_samples；若文档路径不明确，可用 ls 查看目录找到 docx 文件后调用 read_docx_structure。ls 仅用于定位文档路径，严禁浏览与文档无关的其他目录。
3. 此阶段唯一目标是提取 docx 自身的样式和结构信息。如果用户请求中提到了与 docx 不相关的其他文件或目录（如代码、截图、图片等），在本阶段完全忽略它们。你当前阶段的唯一有效输出是样式分析结果，其他意图均无法执行。
4. 拿到样式样本后，用简短中文列出你建议的正文、章节标题、表格字段名、表格填写值等 sample_id 与文档结构概述，并提示用户核对。
5. 列出样式建议和结构概述后，你必须立刻停止回答并等待用户确认！不要继续查看其他目录或文件，不要谈及草稿生成或下一阶段工作。
""".strip()
    elif state == MD_DRAFT:
        state_rule = """
当前状态：Markdown 草稿生成。
你的任务：根据第一阶段确定的样式特征与用户的需求内容，编写出用于填入 Word 的 Markdown 草稿文件。
规则：
1. 你现在只能生成、读取和解析 Markdown 草稿，不能编辑 docx。
2. 请用 write_markdown_draft 按文档区域生成 Markdown 片段，保存到 out/drafts；不要写成包含全流程说明的单个自由草稿。
3. 长正文块可以单独生成 Markdown 文件，例如 experiment_platform.md 等。
4. 每个片段只写最终要进入 Word 的内容，不要包含编辑计划。
5. 如果需要插入图片，草稿中应使用标准 Markdown 图片语法：![描述|对齐方式](图片路径)，对齐方式支持 left/center/right，默认 center。例如：![图表说明|center](out/media/image.png)。先用 analyze_image_content 理解图片内容再写描述，不要仅凭文件名猜测。
6. 如需参考外部代码、报告 md 文件或测试用例等内容作为草稿素材，使用 read 工具读取。
7. 写完后用 read_markdown_draft 或 parse_markdown_draft 展示草稿结构，方便用户确认。
8. 列出草稿结构后，你必须立刻停止回答，等待用户审核草稿。用户没有确认前，不要尝试写入 Word，也不要进入下一阶段。
""".strip()
    else:
        state_rule = """
当前状态：Word 写入与编译。
你的任务：将用户确认的 Markdown 草稿通过编译器写入并替换到 Word 模板对应的位置，最后进行比对验证。
规则：
1. 你现在只能读取 Word 结构、解析 Markdown 片段、调用 markdown_to_word 编译写入，并用 diff_docx 验证。
2. 写入前用 read_docx_structure 确认目标位置，用 parse_markdown_draft 确认 Markdown block_id/support/diagnostics。
3. 普通正文写入只用 write_markdown_to_paragraph（支持段落、标题、列表、图片、表格流式编译与自动生成）；表格单元格写入只用 write_markdown_to_table_cell。
4. 填充或替换占位段落时，用 write_markdown_to_paragraph 的 mode=replace；需要追加内容时使用 mode=after。
5. 一个 Markdown 文件有多个区域时，用 include_block_ids 或 line_start/line_end 选择局部块。
6. 不要引用 markdown_to_word 返回的 temporary_output_path；多步编辑应放在同一次 markdown_to_word.actions 中。
7. 如果 Markdown 片段不适合写入，可以用 write_markdown_draft 修订草稿，但不能绕过 markdown_to_word 直接编辑 docx。
8. 写入后必须调用 diff_docx 验证变化。
""".strip()

    return f"{state_rule}\n\n当前可用工具：\n{render_tools_prompt(available_tool_schemas)}"


SYSTEM_PROMPT = f"""
你是一个精细 DOCX 编辑 agent。

目标：
1. 先读取文档结构或查找锚点，不要盲改。
2. 插入文字时优先保留原 run 格式。
3. 编辑后必须调用 diff_docx 验证变化。
4. 只解释和用户请求相关的变化，注意区分 word/document.xml 的业务变化和 Office 保存噪声。
5. 表格 action 的 table_index 按 //w:tbl 全文计数，嵌套表格也会计数；调用前必须用 read_docx_structure 返回的 depth、父表格坐标、direct_text 确认目标表格、行、列。普通正文 action 使用 write_markdown_to_paragraph（支持段落、标题、列表、图片、表格等所有元素在段落流中的动态编译与自动创建），必须同时传入 paragraph_index 和 anchor_text 定位，以防文本错位插入。
6. 工具由程序按当前状态动态提供。你只能调用当前可见工具，不要臆造不可见工具。
7. 当需要理解图表、截图、排版样式等图片视觉内容时，使用 analyze_image_content 进行多模态识图确认，不要仅凭文件名猜测图片内容。
8. 当需要查看外部代码、Markdown 文档或其他文本文件内容时，使用 read 工具。大文件用 offset/limit 分段读取，每次不超过 500 行以免上下文溢出。
""".strip()


def main():
    adapter = LLMClientAdapter()
    model = adapter.get_model_name()
    thinking_type = adapter.get_thinking_type()
    log_path = create_log_file()

    print("=" * 60)
    print("DOCX Agent Demo - lxml + zipfile")
    print("=" * 60)
    print(f"运行日志: {log_path}")
    print("示例需求：")
    print("把 文档格式测试/cases/insert_text_001/docx/实验报告模板_v3_insert_text_001.docx 中的“依据实验指导书”后插入“测试文本”，另存为 out/demo.docx，并对比原文档。")
    print("输入 quit 或 exit 结束。")
    print("=" * 60)

    user_input = read_user_input("请输入你的文档编辑需求：\n")
    while user_input == "":
        print("需求不能为空")
        user_input = read_user_input("请输入你的文档编辑需求：\n")
    if user_input is None:
        append_log(log_path, "用户退出", {"phase": "before_first_request"})
        print("已退出。")
        return

    provider = adapter.get_provider()
    start_config = {
        "provider": provider,
        "model": model,
        "tool_count": len(TOOLS_SCHEMA),
    }
    if provider == "deepseek":
        start_config["thinking_type"] = adapter.get_thinking_type()
    elif provider == "sensenova":
        start_config["reasoning_effort"] = adapter.get_reasoning_effort()

    append_log(
        log_path,
        "启动配置",
        start_config,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]

    workflow_state = STYLE_REVIEW
    round_index = 0
    while True:
        round_index += 1
        current_tool_schemas = tool_schemas_for_state(workflow_state)
        current_tool_names = tool_names(current_tool_schemas)
        # 合并系统提示词以满足 API 规范：要求只有一个系统提示词且必须置于最前（例如商汤模型）
        combined_system = f"{SYSTEM_PROMPT}\n\n{state_prompt(workflow_state, current_tool_schemas)}"
        request_messages = [{"role": "system", "content": combined_system}] + messages[1:]
        print(f"\n第 {round_index} 轮：正在请求模型 {model} ...", flush=True)
        print(f"当前状态: {workflow_state}，可用工具数: {len(current_tool_schemas)}", flush=True)
        try:
            provider = adapter.get_provider()
            req_log = {
                "provider": provider,
                "model": model,
                "workflow_state": workflow_state,
                "message_count": len(request_messages),
                "tool_names": sorted(current_tool_names),
            }
            if provider == "deepseek":
                req_log["thinking_type"] = adapter.get_thinking_type()
            elif provider == "sensenova":
                req_log["reasoning_effort"] = adapter.get_reasoning_effort()

            append_log(
                log_path,
                f"第 {round_index} 轮模型请求",
                req_log,
            )
            response = adapter.create_chat_completion(
                messages=request_messages,
                tools=current_tool_schemas
            )
        except APITimeoutError as exc:
            print("\n模型请求超时。可以先检查网络、base_url、模型名，或设置更长超时：")
            print("$env:OPENAI_TIMEOUT_SECONDS=\"120\"")
            print(f"错误信息: {exc}")
            append_log(log_path, "模型请求超时", {"error_type": type(exc).__name__, "message": str(exc)})
            return
        except APIConnectionError as exc:
            print("\n无法连接到模型服务。请检查网络、代理、OPENAI_BASE_URL/DeepSeek base_url。")
            print(f"错误信息: {exc}")
            append_log(log_path, "模型连接错误", {"error_type": type(exc).__name__, "message": str(exc)})
            return
        except APIError as exc:
            print("\n模型服务返回错误。请检查 API key、模型名、thinking/tool calling 是否兼容。")
            print(f"错误信息: {exc}")
            append_log(log_path, "模型服务错误", {"error_type": type(exc).__name__, "message": str(exc)})
            return
        except Exception as exc:
            print("\n请求模型时发生未知错误。")
            print(f"错误类型: {type(exc).__name__}")
            print(f"错误信息: {exc}")
            append_log(log_path, "模型未知错误", {"error_type": type(exc).__name__, "message": str(exc)})
            return

        msg = response.choices[0].message
        msg_dict = msg.model_dump(exclude_none=True)
        messages.append(msg_dict)
        append_log(log_path, f"第 {round_index} 轮模型响应", msg_dict)

        reasoning_content = getattr(msg, "reasoning_content", None) or msg_dict.get("reasoning_content")
        if reasoning_content:
            print("\n[模型思考摘要]")
            print(reasoning_content[:1200])

        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                args = tool_call.function.arguments
                print(f"\n调用工具: {name}（详情见日志）")
                append_log(log_path, f"调用工具: {name}", {"tool": name, "arguments": args})
                if name not in current_tool_names:
                    result = json.dumps(
                        {
                            "status": "error",
                            "tool": name,
                            "message": f"tool is not allowed in current state: {workflow_state}",
                        },
                        ensure_ascii=False,
                    )
                else:
                    try:
                        result = call_tool(name, args)
                    except Exception as exc:
                        result = json.dumps(
                            {
                                "status": "error",
                                "tool": name,
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            },
                            ensure_ascii=False,
                        )
                append_log(log_path, f"工具结果: {name}", result)
                print(f"工具状态: {tool_status(result)}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
            continue

        content_stripped = (msg.content or "").strip()
        if len(content_stripped) < 200:
            if workflow_state == STYLE_REVIEW:
                guidance = "你当前处于样式审核阶段，请基于已读取的文档信息直接输出样式分析结果（列出 sample_id 与对应格式特征），不要尝试查看其他目录或文件。"
            elif workflow_state == MD_DRAFT:
                guidance = "请直接输出 Markdown 草稿内容或给出下一步草稿计划。"
            else:
                guidance = "请基于当前可用工具直接执行操作或给出分析结果。"
            print(f"\n[自动引导] {guidance}")
            append_log(log_path, "空响应自动引导", {"workflow_state": workflow_state, "content_length": len(content_stripped)})
            messages.append({"role": "user", "content": guidance})
            continue

        print("\n" + "=" * 60)
        print("最终回复")
        print("=" * 60)
        print(msg.content)
        append_log(log_path, "最终回复", msg.content)

        if workflow_state == STYLE_REVIEW:
            approved = read_yes_no("\n是否确认样式审核并进入编辑阶段？输入 Y 继续，N 留在审核阶段：\n")
            if approved is None:
                append_log(log_path, "用户退出", {"phase": "style_review_approval", "round_index": round_index})
                print("已退出。")
                break
            if approved:
                workflow_state = MD_DRAFT
                append_log(log_path, "状态切换", {"to": workflow_state, "reason": "user_approved_style_review"})
                print("已进入 Markdown 草稿阶段。")
                continue_message = "用户已确认样式审核结果。请基于最初任务和当前上下文，先生成 Markdown 草稿并保存到 out/drafts，然后读取或解析草稿供用户审核；不要编辑 docx。"
                append_log(log_path, "自动继续 Markdown 草稿", continue_message)
                messages.append({"role": "user", "content": continue_message})
                continue
            else:
                append_log(log_path, "样式审核未通过", {"round_index": round_index})
                user_input = read_user_input("\n请补充你的样式审核建议，或输入 quit/exit 结束：\n")
        elif workflow_state == MD_DRAFT:
            approved = read_yes_no("\n是否确认 Markdown 草稿并进入 Word 写入阶段？输入 Y 继续，N 留在草稿阶段：\n")
            if approved is None:
                append_log(log_path, "用户退出", {"phase": "md_draft_approval", "round_index": round_index})
                print("已退出。")
                break
            if approved:
                workflow_state = WORD_EDITING
                append_log(log_path, "状态切换", {"to": workflow_state, "reason": "user_approved_markdown_draft"})
                print("已进入 Word 写入阶段。")
                continue_message = "用户已确认 Markdown 草稿。请读取 Word 结构并解析 Markdown IR，选择目标表格坐标和 style_mapping，用 markdown_to_word 的 actions 编译写入 Word，最后调用 diff_docx 验证。"
                append_log(log_path, "自动继续 Word 写入", continue_message)
                messages.append({"role": "user", "content": continue_message})
                continue
            else:
                append_log(log_path, "Markdown 草稿未通过", {"round_index": round_index})
                user_input = read_user_input("\n请补充你的 Markdown 草稿修改建议，或输入 quit/exit 结束：\n")
        else:
            user_input = read_user_input("\n请输入下一步需求，或输入 quit/exit 结束：\n")

        while user_input == "":
            print("输入为空；请输入内容，或输入 quit/exit 结束。")
            user_input = read_user_input("\n请输入内容，或输入 quit/exit 结束：\n")
        if user_input is None:
            append_log(log_path, "用户退出", {"phase": "after_assistant_reply", "round_index": round_index})
            print("已退出。")
            break
        append_log(log_path, "用户继续输入", user_input)
        messages.append({"role": "user", "content": user_input})


if __name__ == "__main__":
    main()
