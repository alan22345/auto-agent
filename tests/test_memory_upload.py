import pytest
from fastapi.testclient import TestClient

from tests.fixtures.pdf_fixture import make_pdf_bytes
from web.main import app, memory_sessions


@pytest.fixture(autouse=True)
def _clear_sessions():
    memory_sessions.clear()
    yield
    memory_sessions.clear()


@pytest.fixture(autouse=True)
def _patch_auth():
    from web.main import _require_user, app

    async def _stub():
        return 1

    app.dependency_overrides[_require_user] = _stub
    yield
    app.dependency_overrides.clear()


def test_upload_text_file_returns_source_id():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("notes.md", b"# Hello\n\nSome notes.", "text/markdown")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "source_id" in body
    assert body["char_count"] == len("# Hello\n\nSome notes.")
    assert memory_sessions[body["source_id"]].text.startswith("# Hello")


def test_upload_pdf_file_parsed():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("doc.pdf", make_pdf_bytes("pdf payload text"), "application/pdf")},
    )
    assert resp.status_code == 200
    sid = resp.json()["source_id"]
    assert "pdf payload text" in memory_sessions[sid].text


def test_upload_oversize_rejected():
    client = TestClient(app)
    big = ("x" * 250_000).encode()
    resp = client.post("/memory/upload", files={"file": ("big.txt", big, "text/plain")})
    assert resp.status_code == 400
    assert "too large" in resp.text.lower()


def test_upload_unknown_extension_rejected():
    client = TestClient(app)
    resp = client.post(
        "/memory/upload",
        files={"file": ("weird.xyz", b"whatever", "application/octet-stream")},
    )
    assert resp.status_code == 400
