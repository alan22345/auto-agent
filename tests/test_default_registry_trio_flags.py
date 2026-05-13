from agent.tools import create_default_registry


def test_default_registry_no_trio_tools_by_default():
    r = create_default_registry()
    assert r.get("consult_architect") is None
    assert r.get("record_decision") is None
    assert r.get("request_market_brief") is None


def test_with_consult_architect_flag_adds_only_consult():
    r = create_default_registry(with_consult_architect=True)
    assert r.get("consult_architect") is not None
    assert r.get("record_decision") is None


def test_with_architect_tools_adds_record_and_market_brief():
    r = create_default_registry(with_architect_tools=True)
    assert r.get("record_decision") is not None
    assert r.get("request_market_brief") is not None
    # Architect does NOT get consult_architect — it would be consulting itself.
    assert r.get("consult_architect") is None
