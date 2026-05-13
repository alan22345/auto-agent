from datetime import UTC, datetime

from shared.types import (
    AffectedRoute,
    ArchitectAttemptOut,
    ArchitectDecision,
    ConflictInfo,
    IntentVerdict,
    MemorySaveResult,
    ProposedFact,
    RepairContext,
    RepoData,
    ReviewAttemptOut,
    ReviewCombinedVerdict,
    TrioReviewAttemptOut,
    VerifyAttemptOut,
    WorkItem,
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


def test_verify_attempt_out_instantiates():
    a = VerifyAttemptOut(
        id=1, cycle=1, status="pass",
        started_at=datetime.now(UTC),
    )
    assert a.boot_check is None
    assert a.finished_at is None


def test_review_attempt_out_instantiates():
    a = ReviewAttemptOut(
        id=1, cycle=1, status="pass",
        started_at=datetime.now(UTC),
    )
    assert a.code_review_verdict is None
    assert a.ui_check is None


def test_work_item_defaults():
    w = WorkItem(id="abc", title="Add auth", description="...")
    assert w.status == "pending"
    assert w.assigned_task_id is None


def test_architect_decision_minimal():
    d = ArchitectDecision(action="done")
    assert d.reason is None


def test_architect_decision_awaiting_clarification():
    d = ArchitectDecision(action="awaiting_clarification", question="Which db?")
    assert d.question == "Which db?"


def test_repair_context_round_trip():
    r = RepairContext(ci_log="err", failed_pr_url="https://github.com/x/y/pull/1")
    assert RepairContext(**r.model_dump()) == r


def test_trio_review_attempt_serialises():
    a = TrioReviewAttemptOut(
        id=1, task_id=2, cycle=1, ok=True, feedback="", tool_calls=[],
        created_at=datetime(2026, 5, 13),
    )
    assert a.model_dump()["ok"] is True


def test_architect_attempt_out_has_clarification_fields():
    out = ArchitectAttemptOut(
        id=1, task_id=1, phase="initial", cycle=1,
        reasoning="r", decision=None, consult_question=None,
        consult_why=None, architecture_md_after=None,
        commit_sha=None, tool_calls=[],
        clarification_question="Q1",
        clarification_answer="A1",
        clarification_source="po",
        created_at=datetime.now(UTC),
    )
    assert out.clarification_question == "Q1"
    assert out.clarification_answer == "A1"
    assert out.clarification_source == "po"


def test_repo_data_has_product_brief():
    r = RepoData(id=1, name="x", url="https://github.com/x/y.git",
                 product_brief="# Mission\nBuild X.")
    assert r.product_brief == "# Mission\nBuild X."
