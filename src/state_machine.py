"""
DOCX Agent 状态机转换评估模块。

从 src/agent.py step() 末尾的状态机检查逻辑抽出(Step C 重构):
- 阶段校验 (STYLE_REVIEW 必调工具)
- 审批挂起 (yield_approval)
- 状态推进 (advance)
- 修订 (revise)
- 终态 (done)

设计原则:
- WorkflowTransitions 是**纯函数式**评估, 不持有 agent state
- 输入: stage_called_tools / draft_files_written / pending_feedback / accumulated_content 等
- 输出: TransitionDirective dataclass, 描述"下一步该做什么"
- agent 拿到 directive 后自己负责: 写 workflow_state / _pending_approval、append_user、yield 事件、continue 循环

边界(关键!):
- evaluate_* 在 step() 内部被反复调用, **不跨 step() 重启**
- 当 pending_feedback is None, 函数返回 yield_approval directive
- agent 收到 yield_approval → set _pending_approval + checkpoint + yield wait_approval (**不 return**)
- 生成器被 server.py 的 `async for event in agent.step()` 隐式驱动 resume
- agent 继续执行, 读 _pending_feedback
- 再次 evaluate, 此时 pending_feedback != None, 返回 advance/revise/correct
- 这是 "单次 step() 调用内的两段式评估 + yield"
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

from prompts import STYLE_REVIEW, MD_DRAFT, WORD_EDITING


# === 评估结果 dataclass ===
@dataclass(frozen=True)
class TransitionDirective:
    """状态机评估结果 — 描述"下一步 agent 该做什么"。

    Action 枚举及对应 agent 行为:
      - "correct": 阶段校验失败, append_user(correction) + yield extra_event(系统提示) + continue
      - "yield_approval": 校验通过, 等待用户审批 — set _pending_approval + checkpoint + yield wait_approval (**不 return**, 等 server on_user_feedback 设置 _pending_feedback 后继续)
      - "advance": 用户 approve 通过 — pop stage_called_tools + set workflow_state + append_user(continue_msg) + continue (LLM 重新跑在新 state)
      - "revise": 用户未通过 / 错误指令 — 错误指令 yield extra_event+return; 否则 append_user(fb_text) + continue (LLM 重新跑在本 state)
      - "done": WORD_EDITING 收尾 — yield done + return
    """
    action: Literal["advance", "revise", "correct", "yield_approval", "done"]
    to_state: Optional[str] = None
    user_message: Optional[str] = None
    phase: Optional[str] = None  # for wait_approval yield
    extra_event: Optional[dict] = None  # 透传给 yield 的事件字段 (e.g. content delta, error message, draft_content)
    should_clear_drafts: bool = False  # 修订循环时清空 draft_files_written


# === 状态机评估器 — 纯函数式, 不持有 agent state ===
class WorkflowTransitions:
    """DOCX Agent 三阶段状态机的纯函数式评估器。

    所有方法都是 @staticmethod — 接收参数返回 TransitionDirective。
    agent 在 step() 内部反复调用, 直到拿到非 yield_approval 的 directive。
    """

    @staticmethod
    def evaluate_style_review(
        stage_called_tools: dict,
        pending_feedback: Optional[dict],
    ) -> TransitionDirective:
        """STYLE_REVIEW 阶段收尾评估。

        行为:
        - 校验未通过 (缺 analyze_docx_style_samples / define_style_profile) → return correct
        - pending_feedback is None → return yield_approval (等用户审批)
        - pending_feedback.type != "approve" → return revise + error event
        - approved=True → return advance to MD_DRAFT
        - approved=False → return revise (agent 重新跑 LLM 在 STYLE_REVIEW 状态)
        """
        called = stage_called_tools.get(STYLE_REVIEW, set())
        if "analyze_docx_style_samples" not in called or "define_style_profile" not in called:
            correction_msg = (
                "请先调用 analyze_docx_style_samples 分析模板样式，"
                "再参考分析结果调用 define_style_profile 定义各角色的格式参数，"
                "最后再让用户审核。"
            )
            return TransitionDirective(
                action="correct",
                user_message=correction_msg,
                extra_event={"type": "content", "delta": f"\n\n*[系统提示] {correction_msg}*"},
            )

        # 校验通过 — 等待用户审批
        if pending_feedback is None:
            return TransitionDirective(
                action="yield_approval",
                phase=STYLE_REVIEW,
            )

        # 收到反馈
        if pending_feedback.get("type") != "approve":
            return TransitionDirective(
                action="revise",
                user_message="指令类型应为 approve",
                extra_event={"type": "error", "message": "指令类型应为 approve"},
            )

        approved = pending_feedback.get("approved", False)
        fb_text = pending_feedback.get("feedback", "").strip()
        if approved:
            continue_msg = (
                "用户已确认样式审核结果。请基于最初任务和当前上下文，"
                "先生成 Markdown 草稿并保存到 out/drafts，"
                "然后读取或解析草稿供用户审核；不要编辑 docx。"
            )
            return TransitionDirective(
                action="advance",
                to_state=MD_DRAFT,
                user_message=continue_msg,
                should_clear_drafts=True,  # 新一轮草稿周期, 清空旧 .md 路径
            )
        else:
            return TransitionDirective(
                action="revise",
                user_message=(
                    f"用户未确认样式审核结果，并给出反馈意见：{fb_text}。"
                    "请重新分析样式与结构。"
                ),
            )

    @staticmethod
    def evaluate_md_draft(
        stage_called_tools: dict,
        draft_files_written: list,
        draft_content: str,
        pending_feedback: Optional[dict],
    ) -> TransitionDirective:
        """MD_DRAFT 阶段收尾评估。

        行为:
        - pending_feedback is None → return yield_approval (yield 前 agent 拼好 draft_content)
        - pending_feedback.type != "approve" → return revise + error event
        - approved=True → return advance to WORD_EDITING
        - approved=False → return revise + should_clear_drafts=True (修订循环清空旧草稿)
        """
        # 等待用户审批
        if pending_feedback is None:
            return TransitionDirective(
                action="yield_approval",
                phase=MD_DRAFT,
                # draft_content 透传到 wait_approval event 的 draft_content 字段
                extra_event={"draft_content": draft_content},
            )

        # 收到反馈
        if pending_feedback.get("type") != "approve":
            return TransitionDirective(
                action="revise",
                user_message="指令类型应为 approve",
                extra_event={"type": "error", "message": "指令类型应为 approve"},
            )

        approved = pending_feedback.get("approved", False)
        fb_text = pending_feedback.get("feedback", "").strip()
        if approved:
            continue_msg = (
                "用户已确认 Markdown 草稿。请读取 Word 结构并解析 Markdown IR，"
                "选择目标表格坐标和 style_mapping，用 markdown_to_word 的 actions "
                "编译写入 Word，最后调用 diff_docx 验证。"
            )
            return TransitionDirective(
                action="advance",
                to_state=WORD_EDITING,
                user_message=continue_msg,
            )
        else:
            return TransitionDirective(
                action="revise",
                user_message=(
                    f"用户未通过 Markdown 草稿，修改建议：{fb_text}。"
                    "请利用 write_markdown_draft 修订草稿并展示给用户。"
                ),
                should_clear_drafts=True,  # 修订循环, 清空旧草稿路径
            )

    @staticmethod
    def evaluate_word_editing() -> TransitionDirective:
        """WORD_EDITING 结束 — 直接 yield done, 流结束。

        不需要 pending_feedback 评估, 固定 advance 到 done。
        agent 收到 directive 后: _append_log + _checkpoint + yield done + return。
        """
        return TransitionDirective(action="done")
