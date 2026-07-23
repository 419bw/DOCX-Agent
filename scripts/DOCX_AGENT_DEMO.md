# DOCX Agent 最小可行 Demo

这个 demo 不依赖 `python-docx`，只使用 `lxml + zipfile` 精细编辑 `.docx` 的 OpenXML 源码。

## 入口

- `docx_agent_demo.py`

入口会：

1. 从 `docx_tools.registry` 收集工具函数。
2. 从每个工具文件收集 OpenAI tools schema。
3. 用 `render_tools_prompt()` 把工具说明注入 system prompt。
4. 在模型调用工具后执行本地 Python 函数。

## 工具文件

- `docx_tools/read_docx_structure.py`
  - 读取段落、表格文本和定位信息。
- `docx_tools/find_text.py`
  - 按逻辑段落文本查找字符串，返回段落序号、字符偏移和表格位置。
- `docx_tools/insert_text_at.py`
  - 根据锚点文本和 offset 插入文字。
  - 支持 run 中间拆分、末尾新增 run。
- `docx_tools/insert_text_in_table_cell.py`
  - 向指定表格单元格插入文字。
  - 适合空白单元格。
- `docx_tools/replace_text.py`
  - 按逻辑段落文本替换内容。
  - 支持跨多个 run 的文本替换。
- `docx_tools/delete_text.py`
  - 删除指定文本。
  - 支持跨多个 run 命中，并可清理占位符周围空白。
- `docx_tools/insert_paragraph_after.py`
  - 在包含锚点文本的段落后新增段落。
  - 支持复制前一段、后一段或空样式。
- `docx_tools/set_text_format.py`
  - 对指定文本设置字符格式。
  - 支持清除直接格式、转正文格式、设置颜色、加粗和字号。
- `docx_tools/diff_docx.py`
  - 对比两个 docx 包，输出变化文件和段落文本变化。
- `docx_tools/unzip_docx.py`
  - 解包 docx，方便查看源码。
- `docx_tools/common.py`
  - 公共 XML、ZIP、run、段落工具函数。

## 配置

可以用环境变量：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
$env:OPENAI_BASE_URL="https://api.deepseek.com"
$env:OPENAI_MODEL="deepseek-v4-flash"
python src/docx_agent_demo.py
```

也可以创建 `src/config.json`：

```json
{
  "api_key": "你的 key",
  "base_url": "https://api.deepseek.com"
}
```

## 无模型冒烟测试

```powershell
python -c "from src.docx_tools.insert_text_at import insert_text_at; print(insert_text_at(r'文档格式测试\\cases\\baseline\\docx\\实验报告模板_v3修改蓝色部分即可.docx', r'文档格式测试\\cases\\tool_demo\\demo_insert.docx', '依据实验指导书', '[TOOL-DEMO]', 2))"
```

预期结果：

- 输出新文件：`文档格式测试/cases/tool_demo/demo_insert.docx`
- 修改模式：`split_run`
- 新段落文本：`依据[TOOL-DEMO]实验指导书`

## 格式策略

写入类工具支持 `format_policy`：

- `preserve`：保留原 run 格式，适合标题占位符替换。
- `clear`：清除常见直接字符格式。
- `body`：转成正文格式，至少移除颜色、加粗和高亮。
- `custom`：显式设置颜色、加粗、字号。

常用规则：

- 蓝色提示或占位符替换为正式正文时，使用 `format_policy="body"`。
- 标题占位符替换但保留标题视觉效果时，使用 `format_policy="preserve"`。
- 局部加粗、改颜色、改字号时，使用 `set_text_format`。
