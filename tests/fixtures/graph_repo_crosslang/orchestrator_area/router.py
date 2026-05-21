"""Tiny FastAPI-style backend for the cross-language fixture (Phase 4).

The decorators reference a ``router`` symbol imported below so the
parser emits a real ``imports`` AST edge alongside the route nodes.

A sibling ``schemas`` module is imported as well — that one is internal
to the area so the imports edge resolves to a real ``file:`` node id
(``import fastapi`` does not resolve and is dropped at the pipeline
level since it would render as a phantom edge in the canvas).
"""

import fastapi

from orchestrator_area.schemas import RepoRecord


@router.get("/api/repos")
def list_repos():
    return fastapi.something()


@router.post("/api/repos")
def create_repo(payload):
    return RepoRecord(payload)


@router.get("/api/repos/{id}")
def get_repo(id):
    return {"id": id}
