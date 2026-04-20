"""Tests for the users API."""

import pytest
from app import app, db


@pytest.fixture
def client():
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
        yield client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_create_user(client):
    resp = client.post("/api/users", json={"username": "alice", "email": "alice@test.com"})
    assert resp.status_code == 201
    assert resp.json["username"] == "alice"


def test_list_users(client):
    client.post("/api/users", json={"username": "bob", "email": "bob@test.com"})
    resp = client.get("/api/users")
    assert resp.status_code == 200
    assert len(resp.json) == 1


def test_get_user(client):
    client.post("/api/users", json={"username": "carol", "email": "carol@test.com"})
    resp = client.get("/api/users/1")
    assert resp.status_code == 200
    assert resp.json["username"] == "carol"


def test_duplicate_username(client):
    client.post("/api/users", json={"username": "dave", "email": "dave@test.com"})
    resp = client.post("/api/users", json={"username": "dave", "email": "dave2@test.com"})
    assert resp.status_code == 409
