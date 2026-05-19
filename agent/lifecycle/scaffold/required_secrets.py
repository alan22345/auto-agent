"""Required-secrets manifest module — ADR-019 T6.

Handles:
- Structural validation of per-domain required-secrets manifests.
- Parsing manifest JSON files from the workspace.
- Reading all manifests from the ``.auto-agent/required_secrets/`` directory.
- Reconciling DB ``RepoSecret`` rows with the current union of declared keys
  across all domain manifests.

The "skill" in this codebase is a write-file convention: the domain architect
writes ``.auto-agent/required_secrets/<slug>.json`` during its session, then
the orchestrator calls ``parse_manifest_file`` + ``reconcile`` after the agent
run returns (see ``domain_architect.run``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from agent.lifecycle.scaffold.validators import ValidationResult
from shared import repo_secrets
from shared.database import async_session

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_TEST_KINDS: frozenset[str] = frozenset({"postgres_url", "stripe"})

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_REQUIRED_SECRETS_DIR = ".auto-agent/required_secrets"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RequiredSecretEntry:
    key: str
    purpose: str
    test_kind: str | None = None


@dataclass
class RequiredSecretsManifest:
    domain: str  # slug
    secrets: list[RequiredSecretEntry]


@dataclass
class ReconcileReport:
    promoted: list[str] = field(default_factory=list)  # keys flipped user → architect_required
    demoted: list[str] = field(default_factory=list)  # keys flipped architect_required → user
    created: list[str] = field(default_factory=list)  # new placeholder rows
    unchanged: list[str] = field(default_factory=list)  # already architect_required, same purpose


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_manifest(payload: dict) -> ValidationResult:
    """Validate a parsed-JSON manifest.

    Rules:
    - ``domain`` is a non-empty string.
    - ``secrets`` is a list (possibly empty).
    - Every entry has ``key`` matching ``^[A-Z][A-Z0-9_]*$`` and ≤128 chars.
    - Every entry has ``purpose`` non-empty and ≤120 chars.
    - ``test_kind`` is optional; if present must be in
      ``KNOWN_TEST_KINDS = {"postgres_url", "stripe"}``.
    - No duplicate keys within the manifest (cross-domain duplicates are
      fine — handled at reconcile).
    """
    errors: list[str] = []

    # --- domain ---
    domain = payload.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        errors.append("manifest 'domain' must be a non-empty string")

    # --- secrets list ---
    secrets = payload.get("secrets")
    if not isinstance(secrets, list):
        errors.append("manifest 'secrets' must be a list")
        return ValidationResult(ok=False, errors=errors)

    seen_keys: set[str] = set()

    for idx, entry in enumerate(secrets):
        if not isinstance(entry, dict):
            errors.append(f"secrets[{idx}]: expected a JSON object")
            continue

        # key
        key = entry.get("key", "")
        if not isinstance(key, str) or not key:
            errors.append(f"secrets[{idx}]: 'key' is required and must be a string")
        else:
            if len(key) > 128:
                errors.append(
                    f"secrets[{idx}]: key {key!r} exceeds 128 characters"
                )
            elif not _KEY_RE.match(key):
                errors.append(
                    f"secrets[{idx}]: invalid key {key!r} — must match "
                    "^[A-Z][A-Z0-9_]*$ (uppercase letters, digits, underscores; "
                    "must start with a letter)"
                )
            else:
                if key in seen_keys:
                    errors.append(
                        f"secrets[{idx}]: duplicate key {key!r} within the same manifest"
                    )
                else:
                    seen_keys.add(key)

        # purpose
        purpose = entry.get("purpose", "")
        if not isinstance(purpose, str) or not purpose.strip():
            errors.append(f"secrets[{idx}]: 'purpose' is required and must be non-empty")
        elif len(purpose) > 120:
            errors.append(
                f"secrets[{idx}]: purpose exceeds 120 characters "
                f"({len(purpose)} chars)"
            )

        # test_kind (optional)
        test_kind = entry.get("test_kind")
        if test_kind is not None and test_kind not in KNOWN_TEST_KINDS:
            errors.append(
                f"secrets[{idx}]: unknown test_kind {test_kind!r} — "
                f"must be one of {sorted(KNOWN_TEST_KINDS)}"
            )

    return ValidationResult(ok=not errors, errors=errors)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def parse_manifest_file(path: Path) -> RequiredSecretsManifest:
    """Read ``.auto-agent/required_secrets/<slug>.json``, validate, return manifest.

    Raises ``ValueError`` on validation failures, ``json.JSONDecodeError`` on
    invalid JSON.
    """
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    result = validate_manifest(payload)
    if not result.ok:
        raise ValueError(
            f"required-secrets manifest at {path} is invalid:\n"
            + "\n".join(f"  - {e}" for e in result.errors)
        )

    domain = payload["domain"]
    secrets: list[RequiredSecretEntry] = []
    for entry in payload.get("secrets", []):
        secrets.append(
            RequiredSecretEntry(
                key=entry["key"],
                purpose=entry["purpose"],
                test_kind=entry.get("test_kind"),
            )
        )

    return RequiredSecretsManifest(domain=domain, secrets=secrets)


def read_all_manifests(workspace: Path) -> list[RequiredSecretsManifest]:
    """Read every manifest in ``.auto-agent/required_secrets/``.

    Returns empty list if the directory is absent.  Non-``.json`` files are
    ignored.  Invalid manifests log a warning and are also skipped (so a
    single bad file doesn't abort the whole reconcile pass; the caller that
    *wrote* the file is responsible for validation at write time).
    """
    secrets_dir = workspace / _REQUIRED_SECRETS_DIR
    if not secrets_dir.is_dir():
        return []

    manifests: list[RequiredSecretsManifest] = []
    for json_file in sorted(secrets_dir.glob("*.json")):
        try:
            manifests.append(parse_manifest_file(json_file))
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "scaffold.required_secrets.invalid_manifest",
                path=str(json_file),
                error=str(exc),
            )

    return manifests


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


async def reconcile(
    workspace: Path,
    *,
    repo_id: int,
    organization_id: int,
    session: AsyncSession | None = None,
) -> ReconcileReport:
    """Walk every manifest on disk, union the declared keys, and bring
    ``RepoSecret`` rows into agreement.

    Algorithm:
    - Rows whose key IS in the union and whose source is currently 'user':
      promote to 'architect_required' + set purpose.
    - Rows whose key IS in the union and whose source is already
      'architect_required': leave source, update purpose if changed (upsert);
      if purpose unchanged, mark as unchanged.
    - Rows whose key is NOT in the union and whose source is currently
      'architect_required': demote to 'user', clear purpose. Value preserved.
    - Rows whose key IS in the union but no DB row exists: create a placeholder
      row via ``upsert_architect_required`` (value_enc remains NULL).

    All mutations happen inside a single transaction (one commit at the end).
    """
    report = ReconcileReport()

    # Build the declared set: {key → purpose} from the union of all manifests.
    # The first manifest that declares a key wins the purpose if two domains
    # both declare the same key (cross-domain dedup).
    declared: dict[str, str] = {}
    manifests = read_all_manifests(workspace)
    for manifest in manifests:
        for entry in manifest.secrets:
            if entry.key not in declared:
                declared[entry.key] = entry.purpose

    # Open or reuse a session for the whole reconcile pass.
    _owns_session = session is None
    if _owns_session:
        ctx = async_session()
        session = await ctx.__aenter__()  # type: ignore[assignment]
    else:
        ctx = None  # type: ignore[assignment]

    try:
        # Fetch all current rows for this repo.
        existing_rows: list[dict] = await repo_secrets.list_keys(
            repo_id, organization_id=organization_id, session=session
        )
        existing_by_key: dict[str, dict] = {row["key"]: row for row in existing_rows}

        # --- Process declared keys ---
        for key, purpose in declared.items():
            existing = existing_by_key.get(key)
            if existing is None:
                # No row → create placeholder.
                await repo_secrets.upsert_architect_required(
                    repo_id, key, purpose, organization_id=organization_id, session=session
                )
                report.created.append(key)
            elif existing["source"] == "user":
                # User row → promote.
                await repo_secrets.upsert_architect_required(
                    repo_id, key, purpose, organization_id=organization_id, session=session
                )
                report.promoted.append(key)
            else:
                # Already architect_required — update purpose if changed.
                # Either way this is still `unchanged` (source did not flip).
                if existing.get("purpose") != purpose:
                    await repo_secrets.upsert_architect_required(
                        repo_id, key, purpose, organization_id=organization_id, session=session
                    )
                report.unchanged.append(key)

        # --- Process architect_required rows that are no longer declared ---
        for key, existing in existing_by_key.items():
            if existing["source"] == "architect_required" and key not in declared:
                await repo_secrets.demote_to_user(
                    repo_id, key, organization_id=organization_id, session=session
                )
                report.demoted.append(key)

        # Single commit for the whole reconcile pass.
        await session.commit()  # type: ignore[union-attr]

    finally:
        if _owns_session and ctx is not None:
            await ctx.__aexit__(None, None, None)

    log.info(
        "scaffold.required_secrets.reconcile_complete",
        repo_id=repo_id,
        promoted=report.promoted,
        demoted=report.demoted,
        created=report.created,
        unchanged=report.unchanged,
    )

    return report


__all__ = [
    "KNOWN_TEST_KINDS",
    "ReconcileReport",
    "RequiredSecretEntry",
    "RequiredSecretsManifest",
    "ValidationResult",
    "parse_manifest_file",
    "read_all_manifests",
    "reconcile",
    "validate_manifest",
]
