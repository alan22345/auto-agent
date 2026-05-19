"""Tests for ADR-019 T6 — required-secrets manifest module.

Covers:
- validate_manifest: happy path and all rejection rules
- parse_manifest_file: raises ValueError on validation failures
- read_all_manifests: empty list when dir absent; reads multiple files
- reconcile: all DB-state transition scenarios
- domain_architect_system: prompt includes expected sections and handles empty
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_manifest(domain: str = "billing") -> dict:
    return {
        "domain": domain,
        "secrets": [
            {
                "key": "STRIPE_API_KEY",
                "purpose": "Charge cards via Stripe",
                "test_kind": "stripe",
            },
            {
                "key": "STRIPE_WEBHOOK_SECRET",
                "purpose": "Verify Stripe webhook signatures",
            },
        ],
    }


# ---------------------------------------------------------------------------
# validate_manifest — happy path
# ---------------------------------------------------------------------------


def test_validate_manifest_happy_path():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(_make_valid_manifest())
    assert result.ok
    assert result.errors == []


def test_validate_manifest_empty_secrets_list_is_ok():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest({"domain": "auth", "secrets": []})
    assert result.ok


def test_validate_manifest_no_test_kind_is_ok():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "auth",
            "secrets": [{"key": "DATABASE_URL", "purpose": "Connect to Postgres"}],
        }
    )
    assert result.ok


def test_validate_manifest_known_test_kind_postgres_url():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "auth",
            "secrets": [
                {
                    "key": "DATABASE_URL",
                    "purpose": "Connect to Postgres",
                    "test_kind": "postgres_url",
                }
            ],
        }
    )
    assert result.ok


# ---------------------------------------------------------------------------
# validate_manifest — rejection rules
# ---------------------------------------------------------------------------


def test_validate_manifest_rejects_invalid_key_regex_lowercase():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": "stripe_api_key", "purpose": "Charge cards"}],
        }
    )
    assert not result.ok
    assert any("key" in e.lower() or "regex" in e.lower() or "invalid" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_key_starting_with_digit():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": "1STRIPE", "purpose": "Charge cards"}],
        }
    )
    assert not result.ok


def test_validate_manifest_rejects_key_with_spaces():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": "STRIPE API KEY", "purpose": "Charge cards"}],
        }
    )
    assert not result.ok


def test_validate_manifest_rejects_key_exceeding_128_chars():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    long_key = "A" + "B" * 128  # 129 chars
    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": long_key, "purpose": "Too long key"}],
        }
    )
    assert not result.ok
    assert any("128" in e or "long" in e.lower() or "exceeds" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_empty_purpose():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": "STRIPE_API_KEY", "purpose": ""}],
        }
    )
    assert not result.ok
    assert any("purpose" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_purpose_too_long():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    long_purpose = "x" * 121
    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [{"key": "STRIPE_API_KEY", "purpose": long_purpose}],
        }
    )
    assert not result.ok
    assert any("purpose" in e.lower() or "120" in e for e in result.errors)


def test_validate_manifest_rejects_unknown_test_kind():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [
                {
                    "key": "STRIPE_API_KEY",
                    "purpose": "Charge cards",
                    "test_kind": "unknown_tester",
                }
            ],
        }
    )
    assert not result.ok
    assert any("test_kind" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_duplicate_keys_within_domain():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "billing",
            "secrets": [
                {"key": "STRIPE_API_KEY", "purpose": "Charge cards"},
                {"key": "STRIPE_API_KEY", "purpose": "Duplicate key"},
            ],
        }
    )
    assert not result.ok
    assert any("duplicate" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_missing_domain_field():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest({"secrets": []})
    assert not result.ok
    assert any("domain" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_empty_domain_string():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest({"domain": "", "secrets": []})
    assert not result.ok
    assert any("domain" in e.lower() for e in result.errors)


def test_validate_manifest_rejects_non_list_secrets():
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest({"domain": "billing", "secrets": "not-a-list"})
    assert not result.ok
    assert any("secrets" in e.lower() or "list" in e.lower() for e in result.errors)


def test_validate_manifest_collects_multiple_errors():
    """A manifest with several issues returns all errors at once."""
    from agent.lifecycle.scaffold.required_secrets import validate_manifest

    result = validate_manifest(
        {
            "domain": "",
            "secrets": [
                {"key": "bad-key", "purpose": "x" * 121},
            ],
        }
    )
    assert not result.ok
    assert len(result.errors) >= 2


# ---------------------------------------------------------------------------
# parse_manifest_file
# ---------------------------------------------------------------------------


def test_parse_manifest_file_happy_path(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import parse_manifest_file

    manifest = _make_valid_manifest("billing")
    manifest_file = tmp_path / "billing.json"
    manifest_file.write_text(json.dumps(manifest))

    result = parse_manifest_file(manifest_file)
    assert result.domain == "billing"
    assert len(result.secrets) == 2
    assert result.secrets[0].key == "STRIPE_API_KEY"
    assert result.secrets[0].purpose == "Charge cards via Stripe"
    assert result.secrets[0].test_kind == "stripe"
    assert result.secrets[1].key == "STRIPE_WEBHOOK_SECRET"
    assert result.secrets[1].test_kind is None


def test_parse_manifest_file_raises_on_invalid_json(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import parse_manifest_file

    manifest_file = tmp_path / "bad.json"
    manifest_file.write_text("not valid json")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_manifest_file(manifest_file)


def test_parse_manifest_file_raises_value_error_on_validation_failure(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import parse_manifest_file

    # Invalid: key uses lowercase
    bad_manifest = {
        "domain": "billing",
        "secrets": [{"key": "bad_lowercase", "purpose": "Some purpose"}],
    }
    manifest_file = tmp_path / "billing.json"
    manifest_file.write_text(json.dumps(bad_manifest))

    with pytest.raises(ValueError) as exc_info:
        parse_manifest_file(manifest_file)

    # The error message should reference the validation errors
    assert "billing" in str(exc_info.value).lower() or "key" in str(exc_info.value).lower()


def test_parse_manifest_file_raises_captures_errors_list(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import parse_manifest_file

    bad_manifest = {
        "domain": "billing",
        "secrets": [{"key": "bad-key", "purpose": "x" * 200}],
    }
    manifest_file = tmp_path / "billing.json"
    manifest_file.write_text(json.dumps(bad_manifest))

    with pytest.raises(ValueError) as exc_info:
        parse_manifest_file(manifest_file)

    # The error message should be non-trivial
    assert len(str(exc_info.value)) > 10


# ---------------------------------------------------------------------------
# read_all_manifests
# ---------------------------------------------------------------------------


def test_read_all_manifests_returns_empty_list_when_dir_absent(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import read_all_manifests

    workspace = tmp_path  # no .auto-agent/required_secrets/ here
    result = read_all_manifests(workspace)
    assert result == []


def test_read_all_manifests_returns_empty_list_when_dir_is_empty(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import read_all_manifests

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)

    result = read_all_manifests(tmp_path)
    assert result == []


def test_read_all_manifests_reads_single_manifest(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import read_all_manifests

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(json.dumps(_make_valid_manifest("billing")))

    result = read_all_manifests(tmp_path)
    assert len(result) == 1
    assert result[0].domain == "billing"
    assert len(result[0].secrets) == 2


def test_read_all_manifests_reads_multiple_manifest_files(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import read_all_manifests

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)

    (secrets_dir / "billing.json").write_text(json.dumps(_make_valid_manifest("billing")))
    (secrets_dir / "auth.json").write_text(
        json.dumps(
            {
                "domain": "auth",
                "secrets": [
                    {"key": "JWT_SECRET", "purpose": "Sign JWT tokens"},
                ],
            }
        )
    )
    (secrets_dir / "payments.json").write_text(
        json.dumps(
            {
                "domain": "payments",
                "secrets": [
                    {"key": "PAYPAL_CLIENT_ID", "purpose": "PayPal checkout"},
                    {"key": "PAYPAL_CLIENT_SECRET", "purpose": "PayPal API auth"},
                ],
            }
        )
    )

    result = read_all_manifests(tmp_path)
    assert len(result) == 3
    slugs = {m.domain for m in result}
    assert slugs == {"billing", "auth", "payments"}


def test_validate_manifest_rejects_non_kebab_case_domain():
    """validate_manifest rejects PascalCase or other non-kebab-case domain values."""
    from agent.lifecycle.scaffold import required_secrets

    result = required_secrets.validate_manifest({"domain": "Billing", "secrets": []})
    assert not result.ok
    assert any("kebab-case" in e for e in result.errors)


def test_validate_manifest_rejects_domain_with_underscores():
    """validate_manifest rejects domain slugs containing underscores."""
    from agent.lifecycle.scaffold import required_secrets

    result = required_secrets.validate_manifest({"domain": "billing_service", "secrets": []})
    assert not result.ok
    assert any("kebab-case" in e for e in result.errors)


def test_validate_manifest_accepts_valid_kebab_case_domain():
    """validate_manifest accepts valid kebab-case domain slugs."""
    from agent.lifecycle.scaffold import required_secrets

    result = required_secrets.validate_manifest({"domain": "billing-service", "secrets": []})
    assert result.ok


def test_read_all_manifests_skips_unreadable_file(tmp_path: Path):
    """A file that raises OSError on read is skipped, not propagated."""
    from agent.lifecycle.scaffold import required_secrets

    req_dir = tmp_path / ".auto-agent" / "required_secrets"
    req_dir.mkdir(parents=True)
    good_file = req_dir / "good.json"
    good_file.write_text('{"domain": "good", "secrets": []}')
    bad_file = req_dir / "bad.json"
    bad_file.write_text('{"domain": "bad", "secrets": []}')

    original_read_text = Path.read_text

    def selective_read_text(self, *args, **kwargs):
        if self.name == "bad.json":
            raise PermissionError("denied")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", selective_read_text):
        manifests = required_secrets.read_all_manifests(tmp_path)

    # Only good.json should be loaded; bad.json is skipped
    assert len(manifests) == 1
    assert manifests[0].domain == "good"


def test_read_all_manifests_ignores_non_json_files(tmp_path):
    from agent.lifecycle.scaffold.required_secrets import read_all_manifests

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)

    (secrets_dir / "billing.json").write_text(json.dumps(_make_valid_manifest("billing")))
    (secrets_dir / "README.md").write_text("# Notes")
    (secrets_dir / ".gitkeep").write_text("")

    result = read_all_manifests(tmp_path)
    assert len(result) == 1
    assert result[0].domain == "billing"


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def _make_fake_session(rows: list[dict] | None = None):
    """Build a minimal async session mock for reconcile tests.

    ``rows`` is the list of dicts returned by ``list_keys``.
    The session mock tracks execute / commit calls.
    """
    rows = rows or []

    session = AsyncMock()
    session.commit = AsyncMock()
    session.close = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_reconcile_user_row_promoted_to_architect_required(tmp_path):
    """A 'user' row whose key appears in a manifest is promoted."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [
                    {"key": "STRIPE_API_KEY", "purpose": "Charge cards via Stripe"}
                ],
            }
        )
    )

    # Existing DB row: user-set STRIPE_API_KEY
    existing_rows = [
        {"key": "STRIPE_API_KEY", "set": True, "source": "user", "purpose": None, "updated_at": None}
    ]

    promoted: list[str] = []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        promoted.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return existing_rows

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert "STRIPE_API_KEY" in report.promoted
    assert report.demoted == []
    assert report.created == []


@pytest.mark.asyncio
async def test_reconcile_architect_required_row_demoted_when_key_drops(tmp_path):
    """An 'architect_required' row whose key no longer appears is demoted."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    # Empty manifests dir — key no longer declared
    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "auth.json").write_text(
        json.dumps(
            {
                "domain": "auth",
                "secrets": [{"key": "JWT_SECRET", "purpose": "Sign tokens"}],
            }
        )
    )

    # DB has an architect_required row for STRIPE_API_KEY (dropped from manifests)
    # and JWT_SECRET (still in manifests)
    existing_rows = [
        {"key": "STRIPE_API_KEY", "set": True, "source": "architect_required", "purpose": "Old purpose", "updated_at": None},
        {"key": "JWT_SECRET", "set": False, "source": "architect_required", "purpose": "Sign tokens", "updated_at": None},
    ]

    demoted: list[str] = []

    async def fake_demote(repo_id, key, *, organization_id, session=None):
        demoted.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return existing_rows

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            new=AsyncMock(),
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", side_effect=fake_demote),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert "STRIPE_API_KEY" in report.demoted
    assert "JWT_SECRET" not in report.demoted


@pytest.mark.asyncio
async def test_reconcile_creates_placeholder_row_for_new_key(tmp_path):
    """A key declared in manifests with no existing DB row creates a placeholder."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [
                    {"key": "STRIPE_API_KEY", "purpose": "Charge cards via Stripe"},
                ],
            }
        )
    )

    # No existing DB rows
    created: list[str] = []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        created.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return []

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert "STRIPE_API_KEY" in report.created


@pytest.mark.asyncio
async def test_reconcile_unchanged_row_when_key_stays_architect_required(tmp_path):
    """An architect_required row whose key is still in manifests is unchanged."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [
                    {"key": "STRIPE_API_KEY", "purpose": "Charge cards via Stripe"},
                ],
            }
        )
    )

    existing_rows = [
        {
            "key": "STRIPE_API_KEY",
            "set": False,
            "source": "architect_required",
            "purpose": "Charge cards via Stripe",
            "updated_at": None,
        }
    ]

    upsert_calls: list[str] = []
    demote_calls: list[str] = []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        upsert_calls.append(key)

    async def fake_demote(repo_id, key, *, organization_id, session=None):
        demote_calls.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return existing_rows

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", side_effect=fake_demote),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    # Unchanged purpose → no upsert, no demote
    assert "STRIPE_API_KEY" in report.unchanged
    assert upsert_calls == []
    assert demote_calls == []


@pytest.mark.asyncio
async def test_reconcile_updates_purpose_when_changed(tmp_path):
    """An architect_required row with a changed purpose is upserted."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [
                    {"key": "STRIPE_API_KEY", "purpose": "Updated purpose here"},
                ],
            }
        )
    )

    existing_rows = [
        {
            "key": "STRIPE_API_KEY",
            "set": False,
            "source": "architect_required",
            "purpose": "Old purpose",
            "updated_at": None,
        }
    ]

    upsert_calls: list[str] = []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        upsert_calls.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return existing_rows

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    # Purpose changed → upsert is called, but report classification is `unchanged`
    # (source did not flip — key was already architect_required).
    assert "STRIPE_API_KEY" in upsert_calls
    assert "STRIPE_API_KEY" in report.unchanged
    assert "STRIPE_API_KEY" not in report.promoted


@pytest.mark.asyncio
async def test_reconcile_purpose_change_classified_as_unchanged(tmp_path):
    """An architect_required row whose purpose is updated lands in `unchanged`, not `promoted`."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "auth.json").write_text(
        json.dumps(
            {
                "domain": "auth",
                "secrets": [
                    {"key": "JWT_SECRET", "purpose": "new purpose"},
                ],
            }
        )
    )

    # Existing architect_required row with an old purpose.
    existing_rows = [
        {
            "key": "JWT_SECRET",
            "set": False,
            "source": "architect_required",
            "purpose": "old purpose",
            "updated_at": None,
        }
    ]

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return existing_rows

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            new=AsyncMock(),
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert "JWT_SECRET" in report.unchanged
    assert "JWT_SECRET" not in report.promoted


@pytest.mark.asyncio
async def test_reconcile_commits_exactly_once(tmp_path):
    """All updates happen in a single transaction — commit called once."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [
                    {"key": "STRIPE_API_KEY", "purpose": "Charge cards"},
                    {"key": "STRIPE_WEBHOOK_SECRET", "purpose": "Verify webhooks"},
                ],
            }
        )
    )

    # Two new keys → two upsert calls, one commit
    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return []

    commit_count = {"n": 0}

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            new=AsyncMock(),
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()

        async def counting_commit():
            commit_count["n"] += 1

        mock_session.commit = counting_commit
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert commit_count["n"] == 1


@pytest.mark.asyncio
async def test_reconcile_no_db_writes_with_empty_manifests_dir(tmp_path):
    """Empty manifests directory → no DB writes at all."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    # Dir absent entirely
    upsert_calls: list = []
    demote_calls: list = []

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        upsert_calls.append(key)

    async def fake_demote(repo_id, key, *, organization_id, session=None):
        demote_calls.append(key)

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", side_effect=fake_demote),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        report = await reconcile(tmp_path, repo_id=1, organization_id=7)

    assert upsert_calls == []
    assert demote_calls == []
    assert report.promoted == []
    assert report.demoted == []
    assert report.created == []
    assert report.unchanged == []


@pytest.mark.asyncio
async def test_reconcile_cross_domain_duplicate_keys_are_fine(tmp_path):
    """Two domains declaring the same key is allowed — union dedupes it."""
    from agent.lifecycle.scaffold.required_secrets import reconcile

    secrets_dir = tmp_path / ".auto-agent" / "required_secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "billing.json").write_text(
        json.dumps(
            {
                "domain": "billing",
                "secrets": [{"key": "POSTGRES_URL", "purpose": "DB for billing"}],
            }
        )
    )
    (secrets_dir / "auth.json").write_text(
        json.dumps(
            {
                "domain": "auth",
                "secrets": [{"key": "POSTGRES_URL", "purpose": "DB for auth"}],
            }
        )
    )

    upsert_calls: list[str] = []

    async def fake_upsert(repo_id, key, purpose, *, organization_id, session=None):
        upsert_calls.append(key)

    async def fake_list_keys(repo_id, *, organization_id, session=None):
        return []

    with (
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.list_keys", side_effect=fake_list_keys),
        patch(
            "agent.lifecycle.scaffold.required_secrets.repo_secrets.upsert_architect_required",
            side_effect=fake_upsert,
        ),
        patch("agent.lifecycle.scaffold.required_secrets.repo_secrets.demote_to_user", new=AsyncMock()),
        patch("agent.lifecycle.scaffold.required_secrets.async_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await reconcile(tmp_path, repo_id=1, organization_id=7)

    # Should only create one placeholder row even though two domains declared it
    assert upsert_calls.count("POSTGRES_URL") == 1


@pytest.mark.asyncio
async def test_reconcile_runs_even_when_no_manifest_file_written(tmp_path):
    """Reconcile runs after architect.run regardless of whether the architect wrote
    a manifest file — needed so dropped keys get demoted on revise rounds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    # We test via the domain_architect.run entry-point so we exercise the real
    # orchestration flow.  Patch everything that hits external services.
    from shared.models import Task

    task = MagicMock(spec=Task)
    task.id = 42
    task.repo_id = 1
    task.organization_id = 7
    task.description = "test task"
    task.subtasks = {}
    repo_mock = MagicMock()
    repo_mock.name = "test-repo"
    task.repo = repo_mock

    # Workspace with no required_secrets dir (architect wrote nothing).
    workspace_path = tmp_path
    auto_agent_dir = tmp_path / ".auto-agent"
    auto_agent_dir.mkdir()

    # Pre-create a root ADR so run() doesn't bail early.
    adrs_dir = tmp_path / ".auto-agent" / "adrs"
    adrs_dir.mkdir(parents=True)
    root_adr = tmp_path / ".auto-agent" / "adrs" / "000-system.md"
    root_adr.write_text(
        "# System ADR\n\n"
        "## domains:\n\n"
        "```yaml\n"
        "- name: Auth\n"
        "  slug: auth\n"
        "  scope_summary: Authentication domain\n"
        "```\n"
    )

    reconcile_calls: list = []

    async def fake_reconcile(workspace, *, repo_id, organization_id):
        reconcile_calls.append({"repo_id": repo_id, "organization_id": organization_id})
        from agent.lifecycle.scaffold.required_secrets import ReconcileReport
        return ReconcileReport()

    with (
        patch(
            "agent.lifecycle.scaffold.domain_architect.prepare_scaffold_workspace",
            new=AsyncMock(return_value=str(workspace_path)),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect.home_dir_for_task",
            new=AsyncMock(return_value=str(tmp_path / "home")),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect.domain_grill.run",
            new=AsyncMock(return_value={"status": "summary_written"}),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect.create_agent",
        ) as mock_create_agent,
        patch(
            "agent.lifecycle.scaffold.domain_architect.reconcile",
            side_effect=fake_reconcile,
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect._persist_current_domain_idx",
            new=AsyncMock(),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect.repo_secrets.list_keys",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agent.lifecycle.scaffold.domain_architect.parse_domains",
            return_value=[{"name": "Auth", "slug": "auth", "scope_summary": "Authentication domain"}],
        ),
    ):
        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock()
        mock_create_agent.return_value = mock_agent

        from agent.lifecycle.scaffold import domain_architect
        await domain_architect.run(task)

    # reconcile must have been called even though no manifest file was written.
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["repo_id"] == 1
    assert reconcile_calls[0]["organization_id"] == 7


# ---------------------------------------------------------------------------
# domain_architect_system prompt function
# ---------------------------------------------------------------------------


def test_domain_architect_system_contains_currently_set_secrets():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Billing",
        domain_slug="billing",
        index=2,
        currently_set=["STRIPE_API_KEY", "POSTGRES_URL"],
        already_declared=[],
    )
    assert "Currently set secrets" in prompt
    assert "STRIPE_API_KEY" in prompt
    assert "POSTGRES_URL" in prompt


def test_domain_architect_system_contains_already_declared():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Billing",
        domain_slug="billing",
        index=2,
        currently_set=[],
        already_declared=[("STRIPE_API_KEY", "payments"), ("JWT_SECRET", "auth")],
    )
    assert "Already declared by other domains" in prompt
    assert "STRIPE_API_KEY" in prompt
    assert "payments" in prompt
    assert "JWT_SECRET" in prompt
    assert "auth" in prompt


def test_domain_architect_system_empty_currently_set_shows_none_set():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Auth",
        domain_slug="auth",
        index=1,
        currently_set=[],
        already_declared=[],
    )
    assert "(none set)" in prompt


def test_domain_architect_system_empty_already_declared_shows_none():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Auth",
        domain_slug="auth",
        index=1,
        currently_set=[],
        already_declared=[],
    )
    assert "(none)" in prompt


def test_domain_architect_system_instructs_submit_required_secrets():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Billing",
        domain_slug="billing",
        index=2,
        currently_set=[],
        already_declared=[],
    )
    assert "submit-required-secrets" in prompt


def test_domain_architect_system_instructs_json_path():
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Billing",
        domain_slug="billing",
        index=2,
        currently_set=[],
        already_declared=[],
    )
    assert ".auto-agent/required_secrets/billing.json" in prompt


def test_domain_architect_system_still_contains_core_domain_instructions():
    """The new function must preserve all existing domain architect instructions."""
    from agent.lifecycle.scaffold.prompts import domain_architect_system

    prompt = domain_architect_system(
        domain_name="Auth",
        domain_slug="auth",
        index=1,
        currently_set=[],
        already_declared=[],
    )
    # Core scaffold sections must still be present
    assert "submit-domain-adr" in prompt
    assert "## Scope" in prompt
    assert "## Aggregates" in prompt
