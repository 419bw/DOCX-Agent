"""tool_selector — 工具选用 agent

细致编辑模式 (detail_edit) 的内部模块, 由 agent.py 在 step() 中调用。
参考 src/agents/image_refiner.py 的 sub-agent 模式。

职责:
    1. 接收用户编辑需求 + 文档结构摘要 + 全量工具目录
    2. 调用 LLM (阻塞, 无 tools) 选出最合适的工具子集
    3. 校验 + 合并基础工具, 返回工具名列表
    4. 支持再选 (request_more_tools 触发): 传入当前工具集 + 扩充原因, 只增不减

主 agent 看不到这个模块, 只看到 request_more_tools 工具。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from docx_tools import TOOLS_SCHEMA, TOOLS
from prompts import DETAIL_EDIT_BASE_TOOLS, TOOL_SELECTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def build_tool_catalog() -> str:
    """从 TOOLS_SCHEMA 构建精简工具目录 (name: description, 每行一个)。

    排除基础工具 (DETAIL_EDIT_BASE_TOOLS), 避免选用 agent 重复选择。
    """
    lines = []
    for schema in TOOLS_SCHEMA:
        name = schema["function"]["name"]
        if name in DETAIL_EDIT_BASE_TOOLS:
            continue
        desc = schema["function"].get("description", "")
        # 截断过长描述, 保持目录精简
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _parse_tool_names(text: str) -> list[str]:
    """从 LLM 响应文本中提取 JSON 数组。

    兼容:
    - 纯 JSON: ["a", "b"]
    - markdown 包裹: ```json\n["a", "b"]\n```
    - 前后有多余文字: 找第一个 [ 到最后一个 ]
    """
    text = text.strip()
    # 尝试直接解析
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (json.JSONDecodeError, TypeError):
        pass
    # 尝试提取 markdown code block
    code_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_match:
        try:
            result = json.loads(code_match.group(1).strip())
            if isinstance(result, list):
                return [str(x) for x in result]
        except (json.JSONDecodeError, TypeError):
            pass
    # 兜底: 找第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, list):
                return [str(x) for x in result]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def select_tools(
    llm,
    user_request: str,
    doc_structure: str,
    current_tools: set | None = None,
    reason: str | None = None,
) -> list[str]:
    """调用 LLM 选出工具子集。

    Args:
        llm: LLMClientAdapter 实例 (复用主 agent 的适配器)
        user_request: 用户的编辑需求
        doc_structure: read_docx_structure 的 JSON 输出
        current_tools: 再选时传入当前工具集 (只增不减)
        reason: 再选时传入 request_more_tools 的原因

    Returns:
        工具名列表 (已合并基础工具, 已校验)
    """
    catalog = build_tool_catalog()

    # 构建 user message
    parts = [
        f"用户编辑需求：{user_request}",
        f"\n文档结构摘要：\n{doc_structure[:3000]}",  # 截断避免 prompt 过长
        f"\n可选工具目录：\n{catalog}",
    ]
    if current_tools and reason:
        # 再选: 告知当前工具集和扩充原因
        current_names = sorted(current_tools - DETAIL_EDIT_BASE_TOOLS)
        parts.append(f"\n当前已选用的工具：{json.dumps(current_names, ensure_ascii=False)}")
        parts.append(f"\n编辑 agent 请求扩充工具的原因：{reason}")
        parts.append("\n请在当前已选用工具的基础上，增加需要的工具。不要移除已有工具。")

    user_message = "\n".join(parts)

    messages = [
        {"role": "system", "content": TOOL_SELECTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # 调用 LLM (阻塞, 无 tools)
    try:
        response = llm.create_chat_completion_blocking(messages=messages, tools=None)
        content = ""
        if response.choices:
            msg = response.choices[0].message
            content = getattr(msg, "content", "") or ""
    except Exception as e:
        logger.warning("工具选用 LLM 调用失败: %s, 降级为基础工具集", e)
        return sorted(DETAIL_EDIT_BASE_TOOLS)

    # 解析
    raw_names = _parse_tool_names(content)
    if not raw_names:
        logger.warning("工具选用响应解析失败: %r, 降级为基础工具集", content[:200])
        return sorted(DETAIL_EDIT_BASE_TOOLS)

    # 校验: 过滤不在 TOOLS dict 中的名字
    valid_names = {name for name in raw_names if name in TOOLS}
    invalid = set(raw_names) - valid_names
    if invalid:
        logger.info("工具选用过滤无效名字: %s", sorted(invalid))

    # 合并: 基础工具 + 选用工具 (+ 再选时的当前工具)
    result = valid_names | DETAIL_EDIT_BASE_TOOLS
    if current_tools:
        result = result | current_tools  # 只增不减

    logger.info("工具选用结果: %s", sorted(result))
    return sorted(result)
