"""Phase 3 — verify inspect_ui short-circuits on repeated identical inputs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import verify_primitives


@pytest.mark.asyncio
async def test_inspect_ui_uses_cached_result_when_inputs_unchanged(tmp_path, monkeypatch):
    """A second call with the same (route, intent, screenshot bytes) must not invoke the vision LLM."""

    # Reset module-level cache for hermetic test.
    verify_primitives._INSPECT_UI_CACHE.clear()

    screenshot_bytes = b"fake-png-content-stable"
    vision_calls = {"n": 0}

    async def fake_screenshot(*_a, **_kw):
        return screenshot_bytes, None

    async def fake_vision_judge(*_a, **_kw):
        vision_calls["n"] += 1

        class _R:
            ok = True
            reason = ""

        return _R()

    with (
        patch.object(
            verify_primitives,
            "_capture_route_screenshot",
            new=AsyncMock(side_effect=fake_screenshot),
        ),
        patch.object(
            verify_primitives,
            "_vision_judge_screenshot",
            new=AsyncMock(side_effect=fake_vision_judge),
        ),
    ):
        r1 = await verify_primitives.inspect_ui(
            route="/dash", intent="show dashboard", base_url="http://x"
        )
        r2 = await verify_primitives.inspect_ui(
            route="/dash", intent="show dashboard", base_url="http://x"
        )

    assert r1.ok and r2.ok
    assert vision_calls["n"] == 1, "vision judge was called twice for identical inputs — cache miss"


@pytest.mark.asyncio
async def test_inspect_ui_does_not_cache_across_different_intent(tmp_path):
    """Different intent strings must produce two vision-judge calls."""

    verify_primitives._INSPECT_UI_CACHE.clear()

    vision_calls = {"n": 0}

    async def fake_screenshot(*_a, **_kw):
        return b"fake-png", None

    async def fake_vision_judge(*_a, **_kw):
        vision_calls["n"] += 1

        class _R:
            ok = True
            reason = ""

        return _R()

    with (
        patch.object(
            verify_primitives,
            "_capture_route_screenshot",
            new=AsyncMock(side_effect=fake_screenshot),
        ),
        patch.object(
            verify_primitives,
            "_vision_judge_screenshot",
            new=AsyncMock(side_effect=fake_vision_judge),
        ),
    ):
        await verify_primitives.inspect_ui(route="/x", intent="A", base_url="http://x")
        await verify_primitives.inspect_ui(route="/x", intent="B", base_url="http://x")

    assert vision_calls["n"] == 2
