"""test_state_machine.py — WorkflowTransitions 直接单测

覆盖 evaluate_style_review 的校验逻辑（原无直接测试）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from state_machine import WorkflowTransitions


class TestEvaluateStyleReview:
    def test_missing_both_tools_returns_correct(self):
        """两个工具都没调 → correct."""
        directive = WorkflowTransitions.evaluate_style_review({}, None)
        assert directive.action == "correct"
        assert "analyze_docx_style_samples" in directive.user_message
        assert "define_style_profile" in directive.user_message

    def test_missing_define_style_profile_returns_correct(self):
        """只调了 analyze 没调 define → correct."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"analyze_docx_style_samples"}}, None,
        )
        assert directive.action == "correct"

    def test_missing_analyze_returns_correct(self):
        """只调了 define 没调 analyze → correct."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"define_style_profile"}}, None,
        )
        assert directive.action == "correct"

    def test_both_tools_called_yields_approval(self):
        """两个工具都调了 + 无反馈 → yield_approval."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"analyze_docx_style_samples", "define_style_profile"}},
            None,
        )
        assert directive.action == "yield_approval"
        assert directive.phase == "style_review"

    def test_approved_advances_to_md_draft(self):
        """用户 approve → advance to md_draft."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"analyze_docx_style_samples", "define_style_profile"}},
            {"type": "approve", "approved": True, "feedback": ""},
        )
        assert directive.action == "advance"
        assert directive.to_state == "md_draft"

    def test_rejected_returns_revise(self):
        """用户不 approve → revise."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"analyze_docx_style_samples", "define_style_profile"}},
            {"type": "approve", "approved": False, "feedback": "标题格式不对"},
        )
        assert directive.action == "revise"
        assert "标题格式不对" in directive.user_message

    def test_wrong_feedback_type_returns_revise(self):
        """非 approve 类型 → revise + error event."""
        directive = WorkflowTransitions.evaluate_style_review(
            {"style_review": {"analyze_docx_style_samples", "define_style_profile"}},
            {"type": "wrong_type"},
        )
        assert directive.action == "revise"
        assert directive.extra_event is not None
        assert directive.extra_event.get("type") == "error"
