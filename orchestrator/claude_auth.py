"""Per-user Claude credential vault: path helpers and auth-state utilities."""
from __future__ import annotations

import asyncio
import os
from typing import Literal

from shared.config import settings


def vault_dir_for(user_id: int) -> str:
    """Return the absolute HOME directory for the user's Claude vault.

    The CLI looks for credentials at $HOME/.claude/.credentials.json, so we
    pass this path as HOME when spawning the subprocess.
    """
    return os.path.join(settings.users_data_dir, str(user_id))


def ensure_vault_dir(user_id: int) -> str:
    """Create the user's vault directory (mode 0700) if it doesn't exist.

    Returns the path. Idempotent.
    """
    path = vault_dir_for(user_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs honors mode only on creation; enforce on existing dirs too.
    os.chmod(path, 0o700)
    return path


def fallback_user_id() -> int | None:
    """Return the configured fallback user_id (or None)."""
    return settings.fallback_claude_user_id


def effective_user_id_for(owner_user_id: int | None, owner_status: str) -> int | None:
    """Pick which user's vault should run a task.

    - If the owner has paired credentials, use theirs.
    - Otherwise, if a fallback is configured, use the fallback's.
    - Otherwise, return None (caller will park the task in BLOCKED_ON_AUTH).
    """
    if owner_user_id is not None and owner_status == "paired":
        return owner_user_id
    return fallback_user_id()


async def resolve_home_dir(owner_user_id: int | None) -> str | None:
    """Look up the owner's auth status and return the effective vault path.

    None means: no owner, no fallback, and no paired credential — the caller
    should treat this as 'cannot dispatch' (typically: park in BLOCKED_ON_AUTH).
    """
    from sqlalchemy import select

    from shared.database import async_session
    from shared.models import User

    owner_status = "never_paired"
    if owner_user_id is not None:
        async with async_session() as s:
            result = await s.execute(
                select(User).where(User.id == owner_user_id)
            )
            user = result.scalar_one_or_none()
        if user is not None:
            owner_status = user.claude_auth_status

    target = effective_user_id_for(owner_user_id, owner_status)
    if target is None:
        return None
    return ensure_vault_dir(target)


_AUTH_FAILURE_PATTERNS = (
    "unauthorized",
    "expired",
    "please log in",
    "not logged in",
    "authentication required",
)


async def probe_credentials(
    home_dir: str, timeout: float = 15.0
) -> Literal["paired", "expired"]:
    """Run a minimal `claude` invocation under the given HOME and classify result.

    Returns "paired" on clean exit, "expired" if stderr matches an auth-failure
    pattern. Any other failure (timeout, missing binary, non-auth error) is
    treated as "expired" — better to ask the user to re-pair than silently fail
    a task at run time.
    """
    env = {**os.environ, "HOME": home_dir}
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "ping",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except (TimeoutError, FileNotFoundError):
        return "expired"

    if proc.returncode == 0:
        return "paired"
    err = (stderr or b"").decode("utf-8", errors="replace").lower()
    if any(p in err for p in _AUTH_FAILURE_PATTERNS):
        return "expired"
    # Non-auth failure: still treat as expired so the user is prompted, rather
    # than us inferring the credential is fine when we couldn't confirm it.
    return "expired"
