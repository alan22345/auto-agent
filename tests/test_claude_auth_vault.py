import os
import stat

from orchestrator.claude_auth import ensure_vault_dir, vault_dir_for


def test_vault_dir_for_returns_per_user_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    assert vault_dir_for(42) == str(tmp_path / "42")


def test_ensure_vault_dir_creates_with_0700(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    path = ensure_vault_dir(7)
    assert os.path.isdir(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_ensure_vault_dir_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    p1 = ensure_vault_dir(7)
    p2 = ensure_vault_dir(7)
    assert p1 == p2
