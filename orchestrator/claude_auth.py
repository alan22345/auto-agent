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
    except (asyncio.TimeoutError, FileNotFoundError):
        return "expired"

    if proc.returncode == 0:
        return "paired"
    err = (stderr or b"").decode("utf-8", errors="replace").lower()
    if any(p in err for p in _AUTH_FAILURE_PATTERNS):
        return "expired"
    # Non-auth failure: still treat as expired so the user is prompted, rather
    # than us inferring the credential is fine when we couldn't confirm it.
    return "expired"
