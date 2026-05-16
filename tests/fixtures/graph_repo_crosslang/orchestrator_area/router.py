"""Tiny FastAPI-style backend for the cross-language fixture (Phase 4).

The decorators reference a ``router`` symbol imported below so the
parser emits a real ``imports`` AST edge alongside the route nodes.
"""

import fastapi


@router.get("/api/repos")
def list_repos():
    return fastapi.something()


@router.post("/api/repos")
def create_repo(payload):
    return payload


@router.get("/api/repos/{id}")
def get_repo(id):
    return {"id": id}
