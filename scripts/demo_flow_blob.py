"""Generate a sample (graph_blob, flow_blob) pair for local UI verification.

Runs the Phase 2 analyser pipeline on a small fixture / path, derives
the Phase 1 flow blob, then injects Phase 2-style labels (mocked, no
LLM call) so the Map view renders with real names. Writes the two
blobs to JSON files so the Next.js dev demo can load them statically.

Usage:
    python3.12 scripts/demo_flow_blob.py [PATH]

Writes:
    web-next/public/_demo/graph.json
    web-next/public/_demo/flows.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


async def _build_graph(repo_path: Path) -> dict:
    """Run the analyser pipeline on ``repo_path`` and return the blob dict."""
    from agent.graph_analyzer.pipeline import run_pipeline

    blob = await run_pipeline(
        workspace=str(repo_path),
        commit_sha="demo",
        provider=None,
    )
    return blob.model_dump(mode="json")


def _derive_flows(graph_dict: dict, workspace_root: Path) -> dict:
    from agent.graph_analyzer.flows import derive_flow_blob
    from shared.types import RepoGraphBlob

    blob = RepoGraphBlob.model_validate(graph_dict)
    flow_blob = derive_flow_blob(blob, workspace_root=workspace_root)
    return flow_blob.model_dump(mode="json")


def _inject_demo_labels(flow_dict: dict) -> dict:
    """Stand-in for the Phase 2 labeller — gives flows + capabilities
    human-readable names so the Map view shows something nicer than ids.
    """
    for i, flow in enumerate(flow_dict["flows"]):
        entry = flow["entry_point"]["node_id"]
        kind = flow["entry_point"]["kind"]
        terminal = flow["terminal_kind"]
        flow["name"] = f"{kind.upper()} flow {i + 1}"
        flow["description"] = (
            f"Flow entry {entry!r} → terminal kind {terminal!r}, "
            f"{len(flow['steps'])} steps."
        )
        flow["labeled_at_commit"] = "demo"

    # Single "Demo Capability" grouping all flows for the demo. Phase 2
    # would normally produce 5-12 capabilities via the LLM call; for the
    # local demo a single bucket is fine — the UI exercises LOD 0 → 1
    # → 2 → 3 identically.
    if flow_dict["flows"]:
        flow_dict["capabilities"] = [
            {
                "id": "cap_demo",
                "flow_ids": [f["id"] for f in flow_dict["flows"]],
                "flow_membership_hash": "sha256:demo",
                "name": "Demo Capability",
                "description": "All flows derived from the demo fixture.",
                "labeled_at_commit": "demo",
            }
        ]
    flow_dict["labeler_model"] = "demo-stand-in"
    return flow_dict


def _build_rich_demo() -> tuple[dict, dict]:
    """Hand-built blobs that exercise every LOD + boundary ports.

    The Phase 1+2 derivation against the python fixture produces only
    single-step queue flows (no terminal calls — too thin for a demo).
    For the local UI verification we want richer shapes: multi-step
    chains, branches, cycle-backs, cross-capability links, and a
    sibling flow that shares a step. This synthesised blob mimics what
    the labeller would emit for a realistic repo.
    """
    nodes = [
        {
            "id": "app/auth/google.py::login",
            "kind": "function",
            "label": "login",
            "file": "app/auth/google.py",
            "line_start": 12,
            "line_end": 28,
            "area": "auth",
            "parent": None,
        },
        {
            "id": "lib/oauth.py::validate_token",
            "kind": "function",
            "label": "validate_token",
            "file": "lib/oauth.py",
            "line_start": 40,
            "line_end": 70,
            "area": "auth",
            "parent": None,
        },
        {
            "id": "lib/sessions.py::create",
            "kind": "function",
            "label": "create",
            "file": "lib/sessions.py",
            "line_start": 5,
            "line_end": 30,
            "area": "auth",
            "parent": None,
        },
        {
            "id": "app/auth/email.py::signup",
            "kind": "function",
            "label": "signup",
            "file": "app/auth/email.py",
            "line_start": 8,
            "line_end": 40,
            "area": "auth",
            "parent": None,
        },
        {
            "id": "lib/hash.py::hash_password",
            "kind": "function",
            "label": "hash_password",
            "file": "lib/hash.py",
            "line_start": 12,
            "line_end": 25,
            "area": "auth",
            "parent": None,
        },
        {
            "id": "app/carbon/calc.py::compute_emissions",
            "kind": "function",
            "label": "compute_emissions",
            "file": "app/carbon/calc.py",
            "line_start": 20,
            "line_end": 80,
            "area": "carbon",
            "parent": None,
        },
        {
            "id": "lib/formulas.py::burn_factor",
            "kind": "function",
            "label": "burn_factor",
            "file": "lib/formulas.py",
            "line_start": 4,
            "line_end": 16,
            "area": "carbon",
            "parent": None,
        },
        {
            "id": "lib/db.py::write_record",
            "kind": "function",
            "label": "write_record",
            "file": "lib/db.py",
            "line_start": 100,
            "line_end": 130,
            "area": "shared",
            "parent": None,
        },
        # An unreached node so the Unreached tray has content.
        {
            "id": "lib/dead/legacy.py::old_handler",
            "kind": "function",
            "label": "old_handler",
            "file": "lib/dead/legacy.py",
            "line_start": 1,
            "line_end": 8,
            "area": "shared",
            "parent": None,
        },
    ]
    graph = {
        "commit_sha": "demo1234",
        "generated_at": "2026-05-22T12:00:00+00:00",
        "analyser_version": "demo",
        "areas": [
            {
                "name": "auth",
                "status": "ok",
                "error": None,
                "unresolved_dynamic_sites": 0,
            },
            {
                "name": "carbon",
                "status": "ok",
                "error": None,
                "unresolved_dynamic_sites": 0,
            },
            {
                "name": "shared",
                "status": "ok",
                "error": None,
                "unresolved_dynamic_sites": 0,
            },
        ],
        "nodes": nodes,
        "edges": [],
    }

    flows = [
        {
            "id": "flow_google_login",
            "entry_point": {
                "node_id": "app/auth/google.py::login",
                "kind": "http",
            },
            "terminal_node_id": "lib/sessions.py::create",
            "terminal_kind": "db_write",
            "steps": [
                {
                    "node_id": "app/auth/google.py::login",
                    "depth": 0,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                {
                    "node_id": "lib/oauth.py::validate_token",
                    "depth": 1,
                    "is_branch_root": True,
                    "is_cycle_back": False,
                },
                {
                    "node_id": "lib/sessions.py::create",
                    "depth": 2,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
            ],
            "file_set": [
                "app/auth/google.py",
                "lib/oauth.py",
                "lib/sessions.py",
            ],
            "file_set_hash": "sha256:demo-flow-google",
            "name": "Google OAuth Login",
            "description": (
                "Validates the Google OAuth token, then creates a session"
                " row in the database."
            ),
            "labeled_at_commit": "demo1234",
        },
        {
            "id": "flow_email_signup",
            "entry_point": {
                "node_id": "app/auth/email.py::signup",
                "kind": "http",
            },
            "terminal_node_id": "lib/sessions.py::create",
            "terminal_kind": "db_write",
            "steps": [
                {
                    "node_id": "app/auth/email.py::signup",
                    "depth": 0,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                {
                    "node_id": "lib/hash.py::hash_password",
                    "depth": 1,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                # Shared with flow_google_login → produces a sibling
                # boundary port at LOD 2.
                {
                    "node_id": "lib/sessions.py::create",
                    "depth": 2,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
            ],
            "file_set": [
                "app/auth/email.py",
                "lib/hash.py",
                "lib/sessions.py",
            ],
            "file_set_hash": "sha256:demo-flow-email",
            "name": "Email Signup",
            "description": "Hashes the password and creates a session row.",
            "labeled_at_commit": "demo1234",
        },
        {
            "id": "flow_carbon_calc",
            "entry_point": {
                "node_id": "app/carbon/calc.py::compute_emissions",
                "kind": "queue",
            },
            "terminal_node_id": "lib/db.py::write_record",
            "terminal_kind": "db_write",
            "steps": [
                {
                    "node_id": "app/carbon/calc.py::compute_emissions",
                    "depth": 0,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                {
                    "node_id": "lib/formulas.py::burn_factor",
                    "depth": 1,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                {
                    "node_id": "lib/db.py::write_record",
                    "depth": 2,
                    "is_branch_root": False,
                    "is_cycle_back": False,
                },
                # A back-edge to demonstrate the cycle-back indicator.
                {
                    "node_id": "app/carbon/calc.py::compute_emissions",
                    "depth": 3,
                    "is_branch_root": False,
                    "is_cycle_back": True,
                },
            ],
            "file_set": [
                "app/carbon/calc.py",
                "lib/formulas.py",
                "lib/db.py",
            ],
            "file_set_hash": "sha256:demo-flow-carbon",
            "name": "Compute Emissions",
            "description": (
                "Worker reads a job from the queue, computes carbon"
                " emissions, and persists the result."
            ),
            "labeled_at_commit": "demo1234",
        },
    ]

    capabilities = [
        {
            "id": "cap_auth",
            "flow_ids": ["flow_google_login", "flow_email_signup"],
            "flow_membership_hash": "sha256:demo-cap-auth",
            "name": "Authentication",
            "description": (
                "OAuth + email-password login and session creation."
            ),
            "labeled_at_commit": "demo1234",
        },
        {
            "id": "cap_carbon",
            "flow_ids": ["flow_carbon_calc"],
            "flow_membership_hash": "sha256:demo-cap-carbon",
            "name": "Carbon Calc Engine",
            "description": (
                "Background workers that compute and store carbon-emission"
                " records."
            ),
            "labeled_at_commit": "demo1234",
        },
    ]
    flows_blob = {
        "capabilities": capabilities,
        "flows": flows,
        "unreached": [
            "lib/dead/legacy.py::old_handler",
            "lib/dead/legacy.py::stub_helper",
            "tests/test_old.py::test_thing",
        ],
        "derived_at_commit": "demo1234",
        "deriver_version": "demo",
        "labeler_model": "demo-stand-in",
    }
    return graph, flows_blob


async def main() -> None:
    out_dir = REPO_ROOT / "web-next" / "public" / "_demo"
    out_dir.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1 and sys.argv[1] == "--rich":
        print("Using rich hand-built demo blob (exercises every LOD)")
        graph_dict, flow_dict = _build_rich_demo()
    else:
        target = (
            Path(sys.argv[1]).resolve()
            if len(sys.argv) > 1
            else REPO_ROOT / "tests" / "fixtures" / "graph_repo_python"
        )
        print(f"Building graph for {target}")
        graph_dict = await _build_graph(target)
        print(
            f"  nodes={len(graph_dict['nodes'])} edges={len(graph_dict['edges'])} "
            f"areas={len(graph_dict['areas'])}"
        )

        print("Deriving flows…")
        flow_dict = _derive_flows(graph_dict, target)
        print(
            f"  flows={len(flow_dict['flows'])} "
            f"capabilities={len(flow_dict['capabilities'])} "
            f"unreached={len(flow_dict['unreached'])}"
        )

        flow_dict = _inject_demo_labels(flow_dict)

    (out_dir / "graph.json").write_text(json.dumps(graph_dict, indent=2))
    (out_dir / "flows.json").write_text(json.dumps(flow_dict, indent=2))
    print(f"Wrote {out_dir / 'graph.json'}")
    print(f"Wrote {out_dir / 'flows.json'}")


if __name__ == "__main__":
    asyncio.run(main())
