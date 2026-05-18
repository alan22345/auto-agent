"""Structural validators for scaffold ADRs — ADR-018.

The root-ADR and per-domain-ADR markdown blobs are produced by Claude
agents via skills. We can't trust the model to honour the contract on
every run, so we validate the shape and feed any errors back into the
same session for a retry.

Two validators:
- :func:`validate_root_adr` — Vision section, ≤7 domains, each with a
  name + scope_summary, parseable YAML domains block.
- :func:`validate_domain_adr` — scope/aggregates/public-surface/
  integration-points/affected-routes sections, ≥80-word description.

Plus :func:`parse_domains`, which extracts the domain list out of the
root ADR. The contract is a ```yaml ... ``` block whose top key is
``domains:`` — see ``prompts.ROOT_ARCHITECT_SYSTEM`` for the shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain-list extraction
# ---------------------------------------------------------------------------


_YAML_BLOCK_RE = re.compile(r"```yaml\s*\n([\s\S]*?)\n```", re.MULTILINE)

# Hard cap on domains — ADR-018 §3. The validator surfaces this as an
# error so the architect can re-slice in the next turn instead of the
# orchestrator silently dropping domains.
MAX_DOMAINS = 7


def parse_domains(root_adr_md: str) -> list[dict]:
    """Return the parsed ``domains:`` list from a root ADR.

    Walks every ```yaml ... ``` block in document order and returns the
    list under the first block whose top-level mapping has a ``domains``
    key. Returns an empty list if no such block exists or the YAML is
    malformed.

    Each entry is normalised to a dict with ``name`` (str), ``slug``
    (str), ``scope_summary`` (str). Missing fields default to empty
    strings; the caller decides whether to reject those at validation
    time.
    """

    if not root_adr_md:
        return []

    for match in _YAML_BLOCK_RE.finditer(root_adr_md):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        raw = data.get("domains")
        if not isinstance(raw, list):
            continue
        out: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            slug = str(entry.get("slug", "")).strip()
            if not slug and name:
                # Fallback slug — lowercase, spaces → hyphens.
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            scope_summary = str(entry.get("scope_summary", "")).strip()
            out.append(
                {
                    "name": name,
                    "slug": slug,
                    "scope_summary": scope_summary,
                }
            )
        return out

    return []


# ---------------------------------------------------------------------------
# Root ADR validator
# ---------------------------------------------------------------------------


_VISION_HEADER_RE = re.compile(r"^##\s+Vision\b", re.MULTILINE | re.IGNORECASE)


def validate_root_adr(adr_md: str) -> ValidationResult:
    """Check that the root ADR meets the ADR-018 §3 structural contract.

    Rules enforced here (each failure becomes one entry in
    ``errors`` — the caller surfaces the list back to the architect):

    - ``## Vision`` section is present and non-empty.
    - A ```yaml`` block with a ``domains:`` list parses cleanly.
    - The list has 1..7 entries (``MAX_DOMAINS``).
    - Every entry has a non-empty name AND a non-empty scope_summary.
    - Slugs (after fallback) are unique.
    """

    errors: list[str] = []

    if not adr_md or not adr_md.strip():
        errors.append("root ADR is empty")
        return ValidationResult(ok=False, errors=errors)

    if not _VISION_HEADER_RE.search(adr_md):
        errors.append("missing required section: ## Vision")

    domains = parse_domains(adr_md)
    if not domains:
        errors.append(
            "no `domains:` YAML block found — write the domain list "
            "as a ```yaml block with key `domains:` mapping to a list "
            "of {name, slug, scope_summary} entries"
        )
    else:
        if len(domains) > MAX_DOMAINS:
            errors.append(f"too many domains: {len(domains)} > {MAX_DOMAINS} (ADR-018 §3 hard cap)")
        seen_slugs: set[str] = set()
        for idx, d in enumerate(domains, start=1):
            name = d.get("name") or ""
            slug = d.get("slug") or ""
            scope = d.get("scope_summary") or ""
            if not name:
                errors.append(f"domain {idx}: missing name")
            if not slug:
                errors.append(f"domain {idx} ({name or '?'}): missing slug")
            elif slug in seen_slugs:
                errors.append(f"domain {idx}: duplicate slug '{slug}'")
            else:
                seen_slugs.add(slug)
            if not scope:
                errors.append(f"domain {idx} ({name or slug or '?'}): missing scope_summary")

    return ValidationResult(ok=not errors, errors=errors)


# ---------------------------------------------------------------------------
# Domain ADR validator
# ---------------------------------------------------------------------------


_DOMAIN_REQUIRED_SECTIONS = (
    "Scope",
    "Aggregates",
    "Public surface",
    "Integration points",
    "Affected routes",
)


_SECTION_HEADER_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _section_header_re(name: str) -> re.Pattern[str]:
    if name not in _SECTION_HEADER_RE_CACHE:
        # Match ``## <name>`` (case-insensitive, optional trailing punctuation).
        _SECTION_HEADER_RE_CACHE[name] = re.compile(
            rf"^##\s+{re.escape(name)}\b",
            re.MULTILINE | re.IGNORECASE,
        )
    return _SECTION_HEADER_RE_CACHE[name]


_MIN_SCOPE_WORDS = 80


def _extract_section(adr_md: str, name: str) -> str:
    """Return the body of ``## <name>`` up to the next ``## `` header.

    Whitespace-stripped. Empty string if the section is missing.
    """
    header_re = _section_header_re(name)
    m = header_re.search(adr_md)
    if not m:
        return ""
    start = m.end()
    # Find the next ## header (any name) to bound the section.
    next_hdr = re.search(r"^##\s+\S", adr_md[start:], re.MULTILINE)
    end = start + next_hdr.start() if next_hdr else len(adr_md)
    return adr_md[start:end].strip()


def validate_domain_adr(adr_md: str) -> ValidationResult:
    """Check a domain ADR's structural shape.

    Rules:
    - All five required sections (Scope, Aggregates, Public surface,
      Integration points, Affected routes) have a ``## <name>`` header.
    - The Scope section body is ≥80 whitespace-split words.
    """

    errors: list[str] = []

    if not adr_md or not adr_md.strip():
        errors.append("domain ADR is empty")
        return ValidationResult(ok=False, errors=errors)

    for section in _DOMAIN_REQUIRED_SECTIONS:
        if not _section_header_re(section).search(adr_md):
            errors.append(f"missing required section: ## {section}")

    scope_body = _extract_section(adr_md, "Scope")
    word_count = len(scope_body.split())
    if word_count and word_count < _MIN_SCOPE_WORDS:
        errors.append(
            f"## Scope section is too short: {word_count} words (minimum {_MIN_SCOPE_WORDS})"
        )

    return ValidationResult(ok=not errors, errors=errors)


__all__ = [
    "MAX_DOMAINS",
    "ValidationResult",
    "parse_domains",
    "validate_domain_adr",
    "validate_root_adr",
]
