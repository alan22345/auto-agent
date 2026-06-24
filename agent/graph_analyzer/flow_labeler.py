"""Phase 2 LLM labelling for flows and capabilities (spec §4).

Public entry point is :func:`label_flow_blob`. It composes:

  per-flow labelling   ->  per-capability grouping + labelling
       |                          |
  cache by file_set_hash    cache by flow_membership_hash

The labelled :class:`shared.types.FlowJsonBlob` is returned; the caller
(the recompute endpoint) persists it. The labeller is async and uses
:func:`agent.llm.structured.complete_json` for one-shot JSON output.

Cost discipline:
* Per-flow LLM calls cap source slices at ``MAX_LINES_PER_STEP`` lines.
* Total per-flow prompt tokens are bounded by the slice cap x step count.
* Capability grouping is a single LLM call over all flow summaries.
* Reuses prior labels whose ``file_set_hash`` / ``flow_membership_hash``
  match the input blob -- the cache key contract from Phase 1 §4.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import Capability, Flow, FlowJsonBlob

if TYPE_CHECKING:
    from pathlib import Path

    from agent.llm.base import LLMProvider
    from shared.types import FlowStep, Node

log = structlog.get_logger(__name__)

#: Maximum source-line span included in the LLM prompt for one step.
#: Functions longer than this are truncated head-only — the leading lines
#: tend to carry the signature + docstring + early returns, which is
#: enough signal for naming.
MAX_LINES_PER_STEP = 40


def _load_file_slices(
    workspace_root: Path,
    steps: list[FlowStep],
    nodes_by_id: dict[str, Node],
    *,
    max_lines_per_step: int = MAX_LINES_PER_STEP,
) -> list[dict[str, object]]:
    """Read source slices for each step in *steps* from *workspace_root*.

    Returns a list of records ``{"file", "lines": [start, end], "content"}``,
    one per unique ``(file, line_start, line_end)`` triple. Skips steps
    whose node has no file, no line range, or whose file doesn't exist
    on disk. Line ranges longer than ``max_lines_per_step`` are head-
    truncated.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[dict[str, object]] = []
    for step in steps:
        node = nodes_by_id.get(step.node_id)
        if node is None:
            continue
        if not node.file or node.line_start is None or node.line_end is None:
            continue
        key = (node.file, node.line_start, node.line_end)
        if key in seen:
            continue
        seen.add(key)

        file_path = workspace_root / node.file
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = text.splitlines()
        # File lines are 1-indexed; slice is [start-1, end] (end inclusive).
        start_zero = max(0, node.line_start - 1)
        end_zero = min(len(lines), node.line_end)
        clipped_end = min(end_zero, start_zero + max_lines_per_step)
        content_lines = lines[start_zero:clipped_end]
        content = "\n".join(content_lines) + "\n"
        out.append(
            {
                "file": node.file,
                "lines": [node.line_start, node.line_start + len(content_lines) - 1],
                "content": content,
            },
        )
    return out


#: Maximum tokens for a per-flow naming response. The output is tiny
#: (a name + one sentence), so a tight cap prevents the model from
#: padding with reasoning. Mirrors gap_fill.py's choice.
_FLOW_LABEL_MAX_TOKENS = 256

_FLOW_LABEL_SYSTEM = (
    "You name code flows for a developer-facing repo map. Each flow is a "
    "trace from an entry point (HTTP route, queue handler, CLI command, "
    "scheduled job) to a terminal side effect.\n\n"
    "Given the entry point, terminal kind, and source-code slices, "
    "return JSON exactly:\n"
    '{"name": "<<=4 words, Title Case>", '
    '"description": "<<=25 words, one sentence>"}\n\n'
    "The name should be product-language (what the user does), not "
    'function names. Bad: "login function". Good: "Google OAuth Login".'
)


async def _label_flow(
    provider: LLMProvider,
    flow: Flow,
    slices: list[dict[str, object]],
) -> tuple[str | None, str | None]:
    """Ask the LLM to name *flow* given its source slices.

    Returns ``(name, description)``. Returns ``(None, None)`` if the LLM
    call fails (parse error, empty strings) — caller leaves the flow
    unlabelled rather than fabricating a name.
    """
    payload = {
        "entry_point": flow.entry_point.node_id,
        "entry_kind": flow.entry_point.kind,
        "terminal_kind": flow.terminal_kind,
        "step_labels": [s.node_id for s in flow.steps[:10]],
        "source_slices": slices,
    }
    user_msg = Message(role="user", content=str(payload))

    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_FLOW_LABEL_SYSTEM,
            max_tokens=_FLOW_LABEL_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("flow_label.parse_failed", flow_id=flow.id, error=str(exc))
        return (None, None)

    name = response.get("name") or None
    description = response.get("description") or None
    if not name or not description:
        log.warning("flow_label.empty_response", flow_id=flow.id, response=response)
        return (None, None)
    return (name, description)


_CAPABILITY_LABEL_MAX_TOKENS = 4096

# Path segments that don't carry capability semantics on their own —
# stripped when computing a group key from an entry-point file path.
# Examples:
#   ``app/api/v1/agents/[id]/route.ts``  → ``agents``
#   ``app/api/billing/checkout/route.ts``→ ``billing``
#   ``orchestrator/router.py``           → ``orchestrator``
_GROUP_KEY_SKIP = {
    "",
    "app",
    "api",
    "src",
    "lib",
    "pages",
    "v1",
    "v2",
    "v3",
    "v4",
}


def _path_group_key(entry_node_id: str) -> str:
    """Pick a stable group key from an entry-point node id.

    The id has shape ``<file>::<function>``. We walk the file path
    segment-by-segment, skipping framework / versioning noise and
    Next.js dynamic-segment brackets, and return the first
    "interesting" segment. Falls back to ``"other"`` if everything
    was filtered out.

    Deterministic — no LLM call. Used as the bucket key for the
    URL-prefix capability fallback (see :func:`label_flow_blob`).
    """
    file_path = entry_node_id.split("::", 1)[0]
    parts = file_path.split("/")
    for raw in parts:
        seg = raw.strip()
        # Skip Next.js dynamic segments ("[id]", "[...slug]") and
        # the well-known noise prefixes.
        if seg.startswith("[") and seg.endswith("]"):
            continue
        if seg in _GROUP_KEY_SKIP:
            continue
        # Drop the filename itself — caller wants a directory-level key.
        if "." in seg and seg not in {"."}:
            continue
        return seg
    return "other"

_CAPABILITY_LABEL_SYSTEM = (
    "You group code flows into named capabilities for a developer-facing "
    "repo map. A capability is a coherent set of user-visible behaviours "
    '(e.g. "Authentication", "Carbon Calculation").\n\n'
    "Given a list of flows (each with an id, a flow name, an entry point, "
    "and a terminal kind), return JSON exactly:\n"
    '{"capabilities": [\n'
    '  {"name": "<<=4 words, Title Case>",\n'
    '   "description": "<<=25 words, one sentence>",\n'
    '   "flow_ids": ["<id>", "<id>", ...]},\n'
    "  ...\n"
    "]}\n\n"
    "Rules:\n"
    "- Produce 5 to 12 capabilities total when possible. Fewer is fine if "
    "  the repo is small.\n"
    "- Each flow id appears in exactly one capability.\n"
    '- Flows that don\'t fit any group go into a single "Other" capability.'
)


async def _label_capabilities(
    provider: LLMProvider,
    flows: list[Flow],
) -> list[dict[str, object]]:
    """Ask the LLM to group *flows* into named capabilities.

    Returns a list of dicts ``{name, description, flow_ids}``. On LLM
    failure or empty input returns ``[]``; the caller falls back to the
    Phase 1 single-capability shape.

    Any returned capability that references flow_ids not in the input
    list is dropped (defends against hallucinated ids).
    """
    if not flows:
        return []

    payload = {
        "flows": [
            {
                "id": f.id,
                "name": f.name,
                "entry_point": f.entry_point.node_id,
                "entry_kind": f.entry_point.kind,
                "terminal_kind": f.terminal_kind,
            }
            for f in flows
        ],
    }
    user_msg = Message(role="user", content=str(payload))

    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_CAPABILITY_LABEL_SYSTEM,
            max_tokens=_CAPABILITY_LABEL_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("capability_label.parse_failed", error=str(exc))
        return []

    raw_caps = response.get("capabilities") or []
    valid_ids = {f.id for f in flows}
    out: list[dict[str, object]] = []
    for cap in raw_caps:
        if not isinstance(cap, dict):
            continue
        flow_ids = cap.get("flow_ids", [])
        if not isinstance(flow_ids, list) or not all(fid in valid_ids for fid in flow_ids):
            log.warning(
                "capability_label.drop_unknown_flow_ids",
                cap_name=cap.get("name"),
                flow_ids=flow_ids,
            )
            continue
        if not cap.get("name") or not cap.get("description"):
            continue
        out.append(
            {
                "name": cap["name"],
                "description": cap["description"],
                "flow_ids": flow_ids,
            },
        )
    return out


_PREFIX_NAMING_MAX_TOKENS = 2048

_PREFIX_NAMING_SYSTEM = (
    "You name code capabilities for a developer-facing repo map.\n\n"
    "Each input group has a path prefix (already a stable grouping key) "
    "and three sample flow names from that group. For each group, return "
    "a Title-Case capability name (<= 4 words) and a one-sentence "
    "description (<= 25 words).\n\n"
    "Output JSON:\n"
    '{"groups": [\n'
    '  {"prefix": "<input prefix>",\n'
    '   "name": "<Title Case, <=4 words>",\n'
    '   "description": "<<=25 words>"},\n'
    "  ...\n"
    "]}\n\n"
    "Rules:\n"
    "- Exactly one entry per input group.\n"
    '- Echo each prefix verbatim — do not invent new keys.\n'
    "- Do not include flow_ids."
)


async def _name_capability_groups(
    provider: LLMProvider,
    groups: dict[str, list[Flow]],
) -> dict[str, dict[str, str]]:
    """Ask the LLM to give each path-prefix group a name + description.

    Returns ``{prefix: {"name": str, "description": str}}``. On LLM
    failure returns ``{}``; the caller substitutes the bare prefix as
    the name so the user still sees real groups, just less polished.
    """
    if not groups:
        return {}
    payload = {
        "groups": [
            {
                "prefix": prefix,
                "samples": [f.name or f.id for f in flows[:3]],
            }
            for prefix, flows in sorted(groups.items())
        ],
    }
    user_msg = Message(role="user", content=str(payload))
    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_PREFIX_NAMING_SYSTEM,
            max_tokens=_PREFIX_NAMING_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("capability_label.prefix_naming_parse_failed", error=str(exc))
        return {}

    out: dict[str, dict[str, str]] = {}
    for entry in response.get("groups") or []:
        if not isinstance(entry, dict):
            continue
        prefix = entry.get("prefix")
        name = entry.get("name")
        description = entry.get("description")
        if not (prefix and name and description):
            continue
        if prefix not in groups:  # hallucination guard
            continue
        out[str(prefix)] = {"name": str(name), "description": str(description)}
    return out


async def _label_capabilities_by_path_prefix(
    provider: LLMProvider,
    flows: list[Flow],
) -> list[dict[str, object]]:
    """Deterministic capability grouping by entry-point path prefix.

    Robust fallback for repos whose flow count exceeds what a single LLM
    grouping call can emit (LLM response truncates at the token cap and
    the primary grouper returns no capabilities). The grouping itself
    is pure-Python — only the per-bucket name + description require an
    LLM call, and that response stays O(buckets) regardless of flow
    count.

    Returns the same shape as :func:`_label_capabilities`. Falls back
    to using the bare prefix as the capability name when the LLM fails
    to name the buckets, so the user still sees real groups.
    """
    if not flows:
        return []
    groups: dict[str, list[Flow]] = {}
    for f in flows:
        key = _path_group_key(f.entry_point.node_id)
        groups.setdefault(key, []).append(f)

    names = await _name_capability_groups(provider, groups)

    out: list[dict[str, object]] = []
    for prefix, group_flows in sorted(groups.items()):
        named = names.get(prefix)
        out.append(
            {
                "name": (named or {}).get("name") or prefix.replace("-", " ").title(),
                "description": (named or {}).get("description")
                or f"Flows under the ``{prefix}`` path prefix.",
                "flow_ids": [f.id for f in group_flows],
            },
        )
    return out


def _capability_hash(flow_ids: list[str]) -> str:
    """SHA-256 over sorted comma-joined flow_ids, returns ``"sha256:<hex>"``."""
    joined = ",".join(sorted(flow_ids))
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def _phase1_fallback_capability(flow_ids: list[str]) -> Capability:
    """Build the single 'unlabeled' capability with id ``'unlabeled'``."""
    return Capability(
        id="unlabeled",
        flow_ids=flow_ids,
        flow_membership_hash=_capability_hash(flow_ids),
        name=None,
        description=None,
        labeled_at_commit=None,
    )


async def label_flow_blob(
    blob: FlowJsonBlob,
    prior_blob: FlowJsonBlob | None,
    workspace_root: Path,
    nodes_by_id: dict[str, Node],
    provider: LLMProvider,
    *,
    labeler_model: str = "claude-haiku-4-5",
) -> FlowJsonBlob:
    """Phase 2 entry point: label flows + capabilities in *blob*.

    Reuses prior labels whose ``file_set_hash`` (per-flow) or
    ``flow_membership_hash`` (per-capability) matches the supplied
    *prior_blob*. Falls back to the Phase 1 single-"unlabeled" capability
    shape if the LLM grouping call fails.

    Returns a *new* :class:`FlowJsonBlob`. The input blob is not mutated.
    """
    # Build a lookup of prior flows by id for cache checks.
    prior_flows_by_id: dict[str, Flow] = {}
    if prior_blob is not None:
        prior_flows_by_id = {f.id: f for f in prior_blob.flows}

    labelled_flows: list[Flow] = []
    for flow in blob.flows:
        prior = prior_flows_by_id.get(flow.id)
        if (
            prior is not None
            and prior.file_set_hash == flow.file_set_hash
            and prior.name is not None
            and prior.description is not None
        ):
            labelled_flows.append(
                flow.model_copy(
                    update={
                        "name": prior.name,
                        "description": prior.description,
                        "labeled_at_commit": prior.labeled_at_commit,
                    },
                ),
            )
            continue

        slices = _load_file_slices(workspace_root, flow.steps, nodes_by_id)
        name, description = await _label_flow(provider, flow, slices)
        labelled_flows.append(
            flow.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "labeled_at_commit": blob.derived_at_commit if name else None,
                },
            ),
        )

    # Capability grouping over the now-labelled flows. First-line attempt
    # is the freeform LLM grouping (one call over all flow summaries).
    # When the flow count is large enough that the response truncates at
    # the token cap, the call returns no usable capabilities — at that
    # point the URL-prefix fallback kicks in. That fallback's grouping is
    # deterministic; only the per-bucket name/description requires an LLM
    # call, and that response stays small regardless of flow count.
    cap_dicts = await _label_capabilities(provider, labelled_flows)
    if not cap_dicts and labelled_flows:
        log.info(
            "capability_label.falling_back_to_path_prefix",
            flow_count=len(labelled_flows),
        )
        cap_dicts = await _label_capabilities_by_path_prefix(
            provider, labelled_flows
        )

    if not cap_dicts:
        capabilities = [
            _phase1_fallback_capability([f.id for f in labelled_flows]),
        ]
    else:
        # Build prior capabilities by membership hash for cache.
        prior_caps_by_hash: dict[str, Capability] = {}
        if prior_blob is not None:
            prior_caps_by_hash = {c.flow_membership_hash: c for c in prior_blob.capabilities}

        capabilities = []
        for i, cap in enumerate(cap_dicts):
            flow_ids: list[str] = cap["flow_ids"]  # type: ignore[assignment]
            mh = _capability_hash(flow_ids)
            prior_cap = prior_caps_by_hash.get(mh)
            if prior_cap is not None and prior_cap.name is not None:
                capabilities.append(
                    Capability(
                        id=prior_cap.id,
                        flow_ids=flow_ids,
                        flow_membership_hash=mh,
                        name=prior_cap.name,
                        description=prior_cap.description,
                        labeled_at_commit=prior_cap.labeled_at_commit,
                    ),
                )
            else:
                capabilities.append(
                    Capability(
                        id=f"cap_{i}_{mh[7:15]}",  # stable, derived from hash prefix
                        flow_ids=flow_ids,
                        flow_membership_hash=mh,
                        name=cap["name"],  # type: ignore[arg-type]
                        description=cap["description"],  # type: ignore[arg-type]
                        labeled_at_commit=blob.derived_at_commit,
                    ),
                )

    return FlowJsonBlob(
        capabilities=capabilities,
        flows=labelled_flows,
        unreached=blob.unreached,
        derived_at_commit=blob.derived_at_commit,
        deriver_version=blob.deriver_version,
        labeler_model=labeler_model,
    )


__all__ = [
    "MAX_LINES_PER_STEP",
    "_label_capabilities",
    "_label_capabilities_by_path_prefix",
    "_label_flow",
    "_load_file_slices",
    "_path_group_key",
    "label_flow_blob",
]
