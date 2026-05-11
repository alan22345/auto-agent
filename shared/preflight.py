"""Preflight checks — validates the environment before startup.

Run by both `python run.py` and the Docker container to catch
misconfigurations early with clear error messages.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from shared.config import settings


def check_all() -> None:
    """Run all preflight checks. Exits with error if any fail."""
    errors: list[str] = []
    warnings: list[str] = []

    _check_postgres(errors)
    _check_redis(errors)
    _check_claude_code(errors, warnings)

    if not shutil.which("git"):
        errors.append("git is not installed")

    has_app = (
        settings.github_app_id
        and settings.github_app_private_key
        and settings.github_app_installation_id
    )
    if not settings.github_token and not has_app:
        errors.append(
            "No GitHub auth configured — set GITHUB_TOKEN, or configure a "
            "GitHub App via GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + "
            "GITHUB_APP_INSTALLATION_ID."
        )

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        warnings.append("Telegram not configured — no notifications will be sent")

    if not settings.slack_bot_token:
        warnings.append("Slack not configured — Slack integration disabled")

    if not settings.linear_api_key:
        warnings.append("Linear not configured — Linear integration disabled")

    for w in warnings:
        print(f"  [WARN] {w}")

    if errors:
        print("\n  Preflight failed:\n")
        for e in errors:
            print(f"  [FAIL] {e}")
        sys.exit(1)

    print("  [OK] All preflight checks passed")


def _check_postgres(errors: list[str]) -> None:
    url = settings.database_url
    if not url:
        errors.append("DATABASE_URL is not set")
        return

    # Try a real TCP connection to the host:port in the URL
    import socket
    try:
        # Parse host and port from the URL
        # Format: postgresql+asyncpg://user:pass@host:port/db
        after_at = url.split("@")[-1]  # host:port/db
        host_port = after_at.split("/")[0]  # host:port
        host, _, port_str = host_port.rpartition(":")
        port = int(port_str) if port_str else 5432
        if not host:
            host = host_port

        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
    except Exception as e:
        errors.append(f"Cannot connect to Postgres ({url[:60]}...): {e}")


def _check_redis(errors: list[str]) -> None:
    try:
        import redis
        r = redis.from_url(settings.redis_url, socket_connect_timeout=3)
        r.ping()
    except Exception as e:
        errors.append(f"Cannot connect to Redis at {settings.redis_url}: {e}")


def _check_claude_code(errors: list[str], warnings: list[str]) -> None:
    claude_path = shutil.which("claude")
    if not claude_path:
        errors.append("Claude Code CLI is not installed (run: npm install -g @anthropic-ai/claude-code)")
        return

    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--text"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = (result.stdout + result.stderr).strip()

        # Per-user OAuth pairing (Phase 0+) is the canonical path — each user
        # pairs their own Claude into /data/users/{id}/.claude. The host-level
        # `claude auth status` is only meaningful for the optional shared
        # fallback (settings.fallback_claude_user_id). On macOS the host's
        # auth lives in the Keychain and can't be bind-mounted into a Linux
        # container at all, so a "not authenticated" reading there is normal
        # in dev. Downgrade to a warning so the container can boot; tasks
        # without a paired user will still surface BLOCKED_ON_AUTH at runtime.
        if result.returncode != 0:
            warnings.append(
                "Claude Code (host-level) is not authenticated. "
                "Per-user pairing via /settings/claude is the production path; "
                "this check only matters if you want a shared/fallback Claude."
            )
        elif "max" in output.lower() or "pro" in output.lower() or "email" in output.lower():
            pass
        else:
            warnings.append(f"Claude Code auth status unclear: {output[:100]}")

    except subprocess.TimeoutExpired:
        warnings.append("Claude Code auth check timed out — skipping")
    except Exception as e:
        warnings.append(f"Claude Code check failed: {e} — skipping")
