from shared.types import ConflictInfo, MemorySaveResult, ProposedFact


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
