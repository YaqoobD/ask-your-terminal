import json

import pytest

from core.telemetry import record_answer, record_interaction, reconstruct_flag, summarize


def _answer(path, answer_id, grade, **overrides):
    kwargs = dict(
        answer_id=answer_id,
        intent_hash="hash-1",
        tenant_id="tos_alpha",
        grade=grade,
        grade_reasons=["registry coverage current"],
        intent={"op": "aggregate", "metric": "dwell_time", "time_window": "week 3"},
        sql="select 1",
        params=[],
        prompt_version="intent_v1",
        sanitizer_verdict="pass",
        tokens={"input": 100, "output": 50},
        cost_usd=0.01,
        latency_ms={"extract": 200, "compile": 5, "query": 10},
        cache_tier=None,
    )
    kwargs.update(overrides)
    return record_answer(path, **kwargs)


def test_record_answer_and_interaction_round_trip(tmp_path):
    path = tmp_path / "runs.jsonl"
    _answer(path, "ans-1", "CERTIFIED")
    record_interaction(path, answer_id="ans-1", interaction_kind="export_csv")

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["kind"] == "answer"
    assert rows[0]["answer_id"] == "ans-1"
    assert rows[1]["kind"] == "interaction"
    assert rows[1]["answer_id"] == "ans-1"


def test_unknown_interaction_kind_rejected(tmp_path):
    with pytest.raises(ValueError):
        record_interaction(tmp_path / "runs.jsonl", answer_id="ans-1", interaction_kind="not_a_real_kind")


def test_flag_on_certified_answer_is_recoverable_end_to_end(tmp_path):
    path = tmp_path / "runs.jsonl"
    _answer(path, "ans-1", "CERTIFIED", sql="select value from dwell_time_view", params=["terminal_a"])
    record_interaction(path, answer_id="ans-1", interaction_kind="flag_incorrect_grade", reason="looked wrong to me")

    recovered = reconstruct_flag(path, answer_id="ans-1")
    assert recovered is not None
    assert recovered["answer"]["grade"] == "CERTIFIED"
    assert recovered["answer"]["sql"] == "select value from dwell_time_view"
    assert recovered["answer"]["intent"]["metric"] == "dwell_time"
    assert len(recovered["flags"]) == 1
    assert recovered["flags"][0]["reason"] == "looked wrong to me"


def test_summarize_fixture_log_returns_expected_utility_figures(tmp_path):
    path = tmp_path / "runs.jsonl"

    _answer(path, "a1", "CERTIFIED", asker_id="alice", via_clarify=False)
    _answer(path, "a2", "CERTIFIED", asker_id="bob", via_clarify=True)
    _answer(path, "a3", "QUALIFIED", asker_id="alice")
    _answer(path, "a4", "REFUSE", asker_id="carol")

    record_interaction(path, answer_id="a1", interaction_kind="export_csv")
    record_interaction(path, answer_id="a3", interaction_kind="flag_incorrect_grade", reason="stale")

    summary = summarize(path)

    assert summary["total_answers"] == 4
    assert summary["flag_rate_per_grade"]["QUALIFIED"] == pytest.approx(1.0)
    assert summary["flag_rate_per_grade"]["CERTIFIED"] == pytest.approx(0.0)
    assert summary["export_rate"] == pytest.approx(0.25)
    # a1 is CERTIFIED and not via_clarify; a2 is CERTIFIED but via_clarify.
    assert summary["certified_without_clarify_share"] == pytest.approx(0.25)
    assert sum(summary["weekly_active_askers"].values()) >= 1
