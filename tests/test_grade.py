import pytest

from core.grade import Grade, GradeInputs, Refusal, grade


def test_refusal_forces_refuse_regardless_of_other_inputs():
    inputs = GradeInputs(
        refusal=Refusal(reason="cross-tenant filter", source="tenant"),
        completeness_pct=100.0,
        freshness_hours=1.0,
        freshness_sla_hours=24.0,
    )
    result = grade(inputs)
    assert result.grade == Grade.REFUSE
    assert "tenant" in result.reasons[0]


def test_clarify_forces_clarify():
    result = grade(GradeInputs(clarify="which metric do you mean?"))
    assert result.grade == Grade.CLARIFY


def test_all_checks_pass_gives_certified():
    result = grade(GradeInputs(
        freshness_hours=2.0, freshness_sla_hours=24.0,
        completeness_pct=100.0, window_closed_to_corrections=True,
        gold_match=True, unexplained_remainder_pct=2.0,
    ))
    assert result.grade == Grade.CERTIFIED


def test_stale_freshness_forces_qualified():
    result = grade(GradeInputs(freshness_hours=48.0, freshness_sla_hours=24.0, completeness_pct=100.0))
    assert result.grade == Grade.QUALIFIED
    assert any("stale" in r for r in result.reasons)


def test_low_completeness_forces_qualified():
    result = grade(GradeInputs(freshness_hours=1.0, freshness_sla_hours=24.0, completeness_pct=80.0))
    assert result.grade == Grade.QUALIFIED
    assert any("completeness" in r for r in result.reasons)


def test_window_open_to_corrections_forces_qualified():
    result = grade(GradeInputs(completeness_pct=100.0, window_closed_to_corrections=False))
    assert result.grade == Grade.QUALIFIED
    assert any("open to corrections" in r for r in result.reasons)


def test_gold_mismatch_forces_qualified():
    result = grade(GradeInputs(completeness_pct=100.0, gold_match=False))
    assert result.grade == Grade.QUALIFIED
    assert any("gold" in r for r in result.reasons)


def test_large_unexplained_remainder_forces_qualified():
    result = grade(GradeInputs(completeness_pct=100.0, unexplained_remainder_pct=40.0))
    assert result.grade == Grade.QUALIFIED
    assert any("remainder" in r for r in result.reasons)


def test_no_gold_question_covering_the_intent_does_not_block_certified():
    result = grade(GradeInputs(freshness_hours=1.0, freshness_sla_hours=24.0, completeness_pct=100.0, gold_match=None))
    assert result.grade == Grade.CERTIFIED
