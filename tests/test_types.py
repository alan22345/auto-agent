from shared.types import (
    AffectedRoute,
    ConflictInfo,
    IntentVerdict,
    MemorySaveResult,
    ProposedFact,
    ReviewCombinedVerdict,
)


def test_affected_route_defaults():
    r = AffectedRoute(path="/", label="home")
    assert r.method == "GET"


def test_intent_verdict_serialises():
    v = IntentVerdict(ok=True, reasoning="looks good")
    assert v.model_dump()["tool_calls"] == []


def test_review_combined_shape():
    v = ReviewCombinedVerdict(
        code_review={"verdict": "OK", "reasoning": ""},
        ui_check={"verdict": "SKIPPED", "reasoning": ""},
    )
    assert v.code_review.verdict == "OK"


def test_proposed_fact_defaults():
    pf = ProposedFact(
        row_id="r1", entity="auto-agent", entity_type="project",
        kind="fact", content="hello",
    )
    assert pf.conflicts == []
    assert pf.entity_status == "new"
    assert pf.resolution is None


def test_conflict_info_roundtrip():
    c = ConflictInfo(fact_id="f1", existing_content="old")
    assert c.fact_id == "f1"


def test_memory_save_result_counts():
    r = MemorySaveResult(row_id="r1", ok=True)
    assert r.ok is True
    assert r.error is None
