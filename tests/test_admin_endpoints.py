"""Integration tests for /admin/* user CRUD.

Uses the FastAPI TestClient against an in-process app bound to a
temp SQLite file, with bcrypt cost forced to 4 for speed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """FastAPI TestClient with a fresh SQLite DB + admin user seeded."""
    monkeypatch.setenv("LAI_DB_PATH", str(tmp_path / "lai.db"))
    monkeypatch.setenv("LAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAI_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-at-least-32-bytes-long-xxxx")
    monkeypatch.setenv("HISTORY_SECRET_KEY", "another-test-key-at-least-32-bytes-long-xx")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")

    # Force modules to re-read env.
    import importlib
    from backend import db as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = Path(os.environ["LAI_DB_PATH"])

    from backend import passwords
    import asyncio
    asyncio.run(db_mod.init_db())
    asyncio.run(db_mod.create_user(
        username="root", email="root@example.com",
        password_hash=passwords.hash_password("admin-password"),
        is_admin=True,
    ))

    # Lightweight FastAPI app mounting just admin.router + auth routes.
    # We build our own minimal app to avoid startup-cost of the full main.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend import admin as admin_mod
    from backend.config import AppConfig
    from backend import auth

    app = FastAPI()
    app.state.app_config = AppConfig.load(config_dir=ROOT / "config")
    app.include_router(admin_mod.router)

    # Login handler inline (mirrors main.py path).
    from backend.schemas import LoginRequest, LoginResponse, CreateUserRequest
    @app.post("/auth/login")
    async def _login(body: LoginRequest):
        user = await auth.authenticate(body.username, body.password)
        if not user:
            from fastapi import HTTPException
            raise HTTPException(401, "Invalid username or password")
        cfg = app.state.app_config.auth
        token = auth.issue_session_token(user["id"], cfg)
        from fastapi.responses import JSONResponse
        resp = JSONResponse(LoginResponse(
            ok=True, is_admin=bool(user["is_admin"]), username=user["username"],
        ).model_dump())
        resp.set_cookie(cfg.session.cookie_name, token, path="/")
        return resp

    return TestClient(app)


def _login_as_admin(client):
    r = client.post("/auth/login", json={"username": "root", "password": "admin-password"})
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is True


def test_login_wrong_password_returns_401(app_client):
    r = app_client.post("/auth/login", json={"username": "root", "password": "wrong"})
    assert r.status_code == 401


def test_admin_me_requires_auth(app_client):
    r = app_client.get("/admin/me")
    assert r.status_code == 401


def test_admin_users_crud_flow(app_client):
    _login_as_admin(app_client)

    r = app_client.get("/admin/users")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1  # root

    r = app_client.post("/admin/users", json={
        "username": "alice", "email": "alice@example.com",
        "password": "s3cret-p4ss", "is_admin": False,
    })
    assert r.status_code == 200, r.text
    alice = r.json()
    assert alice["username"] == "alice"
    assert alice["is_admin"] is False

    # Duplicate username -> 409
    r = app_client.post("/admin/users", json={
        "username": "alice", "email": "alice2@example.com", "password": "p4ss",
    })
    # short password -> 400 before duplicate check; test with 8 chars
    r = app_client.post("/admin/users", json={
        "username": "alice", "email": "alice2@example.com", "password": "longenough",
    })
    assert r.status_code == 409

    # Patch password for alice
    r = app_client.patch(f"/admin/users/{alice['id']}", json={"password": "new-strong-pw"})
    assert r.status_code == 200

    # Toggle admin
    r = app_client.patch(f"/admin/users/{alice['id']}", json={"is_admin": True})
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    # Delete
    r = app_client.delete(f"/admin/users/{alice['id']}")
    assert r.status_code == 200

    # Gone
    r = app_client.get("/admin/users")
    assert r.status_code == 200
    assert all(u["id"] != alice["id"] for u in r.json()["data"])


def test_admin_cannot_self_delete(app_client):
    _login_as_admin(app_client)
    me = app_client.get("/admin/me").json()
    users = app_client.get("/admin/users").json()["data"]
    root_id = next(u["id"] for u in users if u["username"] == "root")
    r = app_client.delete(f"/admin/users/{root_id}")
    assert r.status_code == 400


def test_create_user_short_password_rejected(app_client):
    _login_as_admin(app_client)
    r = app_client.post("/admin/users", json={
        "username": "bob", "email": "bob@x.io", "password": "short",
    })
    assert r.status_code == 400
