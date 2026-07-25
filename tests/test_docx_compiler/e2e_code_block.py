"""端到端测试: 真实 markdown_to_word 工具链路

流程: baseline docx (before) + markdown 草稿 → markdown_to_word → after docx
输出到 文档格式测试/cases/code_block_009/docx/ 供 WPS/Word 打开验证。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# monkey-patch workspace guard 到临时目录
import workspace.guard as guard

_tmp = Path(tempfile.mkdtemp(prefix="code_block_e2e_"))
guard.WORKSPACE_ROOT = _tmp / "sessions"

from md_tools.markdown_to_word import markdown_to_word

SESSION_ID = "e2e-code-block-009"
WS = guard.workspace_dir(SESSION_ID)  # 创建 workspace 目录

# ---------- 1. 准备 before docx ----------
BEFORE_SRC = PROJECT_ROOT / "文档格式测试" / "cases" / "baseline" / "docx" / "实验报告模板_v3修改蓝色部分即可.docx"
BEFORE = WS / "before.docx"
shutil.copy2(BEFORE_SRC, BEFORE)
print(f"[before] {BEFORE}")

# ---------- 2. 写 markdown 草稿 ----------
MARKDOWN = """\
下面是一段 Python 代码：

```python
def hello(name: str) -> str:
    return f"Hello, {name}!"

for i in range(5):
    print(hello("World"))
```

下面是一段没有语言标记的代码：

```
plain text line 1
plain text line 2
```

普通段落收尾。
"""
MD_PATH = WS / "drafts" / "draft.md"
MD_PATH.parent.mkdir(parents=True, exist_ok=True)
MD_PATH.write_text(MARKDOWN, encoding="utf-8")
print(f"[markdown] {MD_PATH}")

# ---------- 3. 写最小 style profile ----------
STYLE_PROFILE = {
    "style_samples": [
        {"sample_id": "S001", "paragraph": {}, "run": {}}
    ],
    "role_bindings": {
        "body": "S001",
        "section_heading": "S001",
        "title": "S001",
    },
}
PROFILE_PATH = WS / "style_profile.json"
PROFILE_PATH.write_text(json.dumps(STYLE_PROFILE, ensure_ascii=False), encoding="utf-8")
print(f"[style_profile] {PROFILE_PATH}")

# ---------- 4. 调用 markdown_to_word ----------
result_json = markdown_to_word(
    session_id=SESSION_ID,
    docx_path="before.docx",
    output_path="after.docx",
    markdown_path="draft.md",
    style_profile_path="style_profile.json",
    actions=[
        {
            "type": "write_markdown_to_paragraph",
            "paragraph_index": 11,
            "anchor_text": "依据实验指导书",
            "mode": "after",
        }
    ],
)

result = json.loads(result_json)
print(f"\n[result] status={result.get('status')}")
if result.get("status") != "ok":
    print(f"  message: {result.get('message')}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(1)

for d in result.get("diagnostics", []):
    print(f"  [{d.get('level')}] {d.get('code')}: {d.get('message')}")
print(f"  support_summary: {result.get('support_summary')}")

# ---------- 5. 复制 after 到测试 case 目录 ----------
AFTER_SRC = WS / "after.docx"
CASE_DIR = PROJECT_ROOT / "文档格式测试" / "cases" / "code_block_009" / "docx"
AFTER_DST = CASE_DIR / "实验报告模板_v3_code_block_009_after.docx"
shutil.copy2(AFTER_SRC, AFTER_DST)
print(f"\n[after] {AFTER_DST}")
print("用 WPS/Word 打开查看效果。")

# 清理临时目录
shutil.rmtree(_tmp, ignore_errors=True)
