"""FastAPI routes — internal API for task management."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime

from croniter import croniter
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.auth import (
    create_token,
    current_org_id as current_org_id_dep,
    current_user_id,
    hash_password,
    verify_password,
    verify_token,
)
from orchestrator.deduplicator import find_duplicate_by_source_id, find_duplicate_by_title
from orchestrator.feedback import analyze_patterns, get_feedback_summary, record_outcome
from orchestrator.freeform import promote_task_to_main, revert_task_from_dev
from orchestrator.scoping import scoped
from orchestrator.state_machine import InvalidTransition, get_task, transition
from shared import quotas
from shared.config import settings
from shared.database import get_session
from shared.events import (
    po_analyze,
    publish,
    repo_deleted,
    repo_onboard,
    task_approved,
    task_cleanup,
    task_created,
    task_failed,
    task_feedback,
    task_rejected,
    task_review_approved,
    task_start_planning,
)
from shared.models import (
    FreeformConfig,
    MarketBrief,
    Organization,
    OrganizationMembership,
    Plan,
    Repo,
    ReviewAttempt,
    ScheduledTask,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskHistory,
    TaskMessage,
    TaskSource,
    TaskStatus,
    User,
    VerifyAttempt,
    intake_qa_for_suggestion,
)
from shared.task_channel import task_channel
from shared.types import (
    ChangeEmailRequest,
    CreateUserRequest,
    FeedbackSummary,
    FreeformConfigData,
    LoginRequest,
    LoginResponse,
    MarketBriefResponse,
    OutcomeResponse,
    PlanRead,
    RepoData,
    RepoResponse,
    ReviewAttemptOut,
    ScheduleResponse,
    SecretListResponse,
    SecretPutRequest,
    SecretTestResponse,
    SignupRequest,
    SignupResponse,
    SuggestionData,
    TaskData,
    TaskMessageData,
    TaskMessagePost,
    UsageSummary,
    UserData,
    VerifyAttemptOut,
)

router = APIRouter()

# --- Session cookie ---

COOKIE_NAME = "auto_agent_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days — match JWT default expiry

# Statuses that indicate a task is no longer actively running.
TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED}

# --- Rate limiting ---

TASK_CREATION_RATE_LIMIT = 10  # max tasks per window
TASK_CREATION_WINDOW = 60  # seconds
_task_creation_timestamps: list[float] = []


def _seconds_until_utc_midnight() -> int:
    import datetime as _dt
    now = _dt.datetime.now(_dt.UTC)
    tomorrow = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((tomorrow - now).total_seconds())


def _check_rate_limit() -> None:
    """Raise 429 if task creation rate limit is exceeded."""
    now = time.monotonic()
    # Prune old timestamps
    while _task_creation_timestamps and _task_creation_timestamps[0] < now - TASK_CREATION_WINDOW:
        _task_creation_timestamps.pop(0)
    if len(_task_creation_timestamps) >= TASK_CREATION_RATE_LIMIT:
        raise HTTPException(
            429,
            f"Rate limit exceeded: max {TASK_CREATION_RATE_LIMIT} tasks per {TASK_CREATION_WINDOW}s",
        )
    _task_creation_timestamps.append(now)


# --- Request schemas ---


BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


class CreateTaskRequest(BaseModel):
    title: str = Field(max_length=256)
    description: str = Field(default="", max_length=10000)
    source: TaskSource = TaskSource.MANUAL
    source_id: str = Field(default="", max_length=512)
    repo_name: str | None = Field(default=None, max_length=256)
    created_by_user_id: int | None = None


class TransitionRequest(BaseModel):
    status: TaskStatus
    message: str = Field(default="", max_length=2000)
    plan: str | None = Field(default=None, max_length=50000)


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = Field(default="", max_length=5000)
    # Optional override for the TaskHistory message. Used by the freeform-mode
    # auto-reviewer so its decision (and reasoning) lands in the audit log.
    message: str = Field(default="", max_length=2000)


class RecordOutcomeRequest(BaseModel):
    pr_approved: bool
    review_rounds: int = 0
    feedback_summary: str = Field(default="", max_length=5000)


class RegisterRepoRequest(BaseModel):
    name: str = Field(max_length=256)
    url: str = Field(max_length=2048)
    default_branch: str = Field(default="main", max_length=256)


class CreateScheduleRequest(BaseModel):
    name: str = Field(max_length=256)
    cron_expression: str = Field(max_length=128)  # e.g. "0 9 * * 1" = every Monday 9am
    task_title: str = Field(max_length=256)
    task_description: str = Field(default="", max_length=10000)
    repo_name: str | None = Field(default=None, max_length=256)

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError("Invalid cron expression")
        return v


class DeleteResponse(BaseModel):
    deleted: int


class ToggleResponse(BaseModel):
    id: int
    enabled: bool


class PatternsResponse(BaseModel):
    analysis: str


# --- Auth helpers ---


def _verify_auth_header(authorization: str | None) -> dict:
    """Extract and verify JWT from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    payload = verify_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


def _verify_cookie_or_header(cookie: str | None, authorization: str | None) -> dict:
    """Accept either the session cookie or Authorization: Bearer header."""
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return payload
    return _verify_auth_header(authorization)


async def _get_task_in_org(
    session: AsyncSession, task_id: int, org_id: int,
) -> Task | None:
    """Return the task only if it belongs to the caller's org. None otherwise.

    Calls that look up a single task by ID must go through this helper
    rather than ``select(Task).where(Task.id == task_id)`` so the org
    filter is never omitted.
    """
    result = await session.execute(
        scoped(select(Task).where(Task.id == task_id), Task, org_id=org_id)
    )
    return result.scalar_one_or_none()


async def _get_repo_in_org(
    session: AsyncSession, *, repo_id: int | None = None, name: str | None = None,
    org_id: int,
) -> Repo | None:
    """Return the repo only if it belongs to the caller's org. None otherwise.

    Accepts either ``repo_id`` or ``name`` (mutually exclusive). The name
    lookup also tolerates a ``owner/repo`` suffix match for backwards
    compatibility with how some legacy callers identify repos.
    """
    if repo_id is not None:
        q = select(Repo).where(Repo.id == repo_id)
    elif name is not None:
        q = select(Repo).where(
            (Repo.name == name) | (Repo.name.endswith(f"/{name}"))
        )
    else:
        raise ValueError("repo_id or name is required")
    result = await session.execute(scoped(q, Repo, org_id=org_id))
    return result.scalar_one_or_none()


# --- Auth endpoints ---


async def _resolve_active_org_id(session: AsyncSession, user: User) -> int:
    """Pick the user's active org for a fresh session.

    Strategy: most-recently-active membership wins; ties broken by oldest
    membership (the org they joined first / signed up with). Bumps the
    membership's ``last_active_at`` so the next login lands in the same
    org by default.

    Raises 403 if the user has no memberships — this should be unreachable
    after Phase 2 migration backfill, but failing loud beats silently
    handing out a token with no tenant.
    """
    result = await session.execute(
        select(OrganizationMembership)
        .where(OrganizationMembership.user_id == user.id)
        .order_by(
            OrganizationMembership.last_active_at.desc().nullslast(),
            OrganizationMembership.created_at.asc(),
        )
        .limit(1)
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=403,
            detail="User has no organization memberships — contact support",
        )
    membership.last_active_at = datetime.now(UTC)
    return int(membership.org_id)


@router.post("/auth/login")
async def login(req: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)):
    # Allow login by either username (legacy admin) or email (self-serve).
    result = await session.execute(
        select(User).where(
            (User.username == req.username) | (User.email == req.username)
        )
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Self-serve users (those with an email on file) must verify it before
    # logging in. Legacy admin/seeded users with email=NULL bypass this.
    if user.email and user.email_verified_at is None:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Check your inbox for the verification link.",
        )
    user.last_login = datetime.now(UTC)
    org_id = await _resolve_active_org_id(session, user)
    await session.commit()
    token = create_token(user_id=user.id, username=user.username, current_org_id=org_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        path="/",
    )
    return LoginResponse(
        token=token,
        user=UserData(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            created_at=user.created_at.isoformat() if user.created_at else None,
            last_login=user.last_login.isoformat() if user.last_login else None,
            claude_auth_status=user.claude_auth_status,
            claude_paired_at=(
                user.claude_paired_at.isoformat() if user.claude_paired_at else None
            ),
        ),
    )


@router.get("/auth/me")
async def get_me(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
):
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    result = await session.execute(select(User).where(User.id == payload["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return UserData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        created_at=user.created_at.isoformat() if user.created_at else None,
        last_login=user.last_login.isoformat() if user.last_login else None,
        claude_auth_status=user.claude_auth_status,
        claude_paired_at=(
            user.claude_paired_at.isoformat() if user.claude_paired_at else None
        ),
        telegram_chat_id=user.telegram_chat_id,
        slack_user_id=user.slack_user_id,
    )


class _MessagingLinkRequest(BaseModel):
    """Body for the messaging-platform link endpoints — pass null to clear."""

    value: str | None = Field(default=None, max_length=64)


@router.patch("/auth/me/telegram")
async def set_my_telegram_chat_id(
    body: _MessagingLinkRequest,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
):
    """Link or unlink the caller's Telegram chat. The user discovers their
    chat_id via the bot's ``/whoami`` command, then pastes it here."""
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    new_value = body.value.strip() if body.value else None
    await session.execute(
        sql_update(User)
        .where(User.id == payload["user_id"])
        .values(telegram_chat_id=new_value or None)
    )
    await session.commit()
    return {"ok": True, "telegram_chat_id": new_value}


@router.patch("/auth/me/slack")
async def set_my_slack_user_id(
    body: _MessagingLinkRequest,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
):
    """Link or unlink the caller's Slack user ID. Filled in automatically
    by the Slack integration when a teammate first DMs the bot, but exposed
    here so admins can correct mismatches."""
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    new_value = body.value.strip() if body.value else None
    await session.execute(
        sql_update(User)
        .where(User.id == payload["user_id"])
        .values(slack_user_id=new_value or None)
    )
    await session.commit()
    return {"ok": True, "slack_user_id": new_value}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


# --- Self-serve signup + email verification (Phase 1 multi-tenant) ---


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _username_from_email(local_part: str) -> str:
    """Sanitise the local-part of an email into a candidate username.
    Falls back to ``user`` if nothing alphanumeric remains."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", local_part).lower()
    return cleaned or "user"


async def _allocate_username(session: AsyncSession, base: str) -> str:
    """Return ``base`` if free, otherwise ``base2``, ``base3``, ... until free."""
    candidate = base
    suffix = 1
    while True:
        existing = await session.execute(
            select(User).where(User.username == candidate)
        )
        if existing.scalar_one_or_none() is None:
            return candidate
        suffix += 1
        candidate = f"{base}{suffix}"


@router.post("/auth/signup", status_code=201)
async def signup(
    req: SignupRequest,
    session: AsyncSession = Depends(get_session),
) -> SignupResponse:
    """Create a new user from email + password + display_name.

    Always returns 201 with ``verification_sent`` indicating whether the
    Resend dispatch succeeded. Even if delivery fails we still create the
    user — the operator can pull the verify URL out of the logs.
    """
    import secrets as _stdlib_secrets

    email = _normalise_email(req.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Invalid email address")

    existing = await session.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "An account with this email already exists")

    local = email.split("@", 1)[0]
    username = await _allocate_username(session, _username_from_email(local))
    token = _stdlib_secrets.token_urlsafe(32)

    # Phase 2 — every signup gets a personal org with the user as owner.
    # The slug is the email-local part plus a 4-char random suffix to avoid
    # collisions between e.g. alice@a.com and alice@b.com.
    org_slug = f"{_username_from_email(local)[:24]}-{_stdlib_secrets.token_hex(2)}"
    free_plan_q = await session.execute(select(Plan).where(Plan.name == "free"))
    free_plan = free_plan_q.scalar_one()  # migration 029 guarantees one row exists
    org = Organization(name=req.display_name.strip(), slug=org_slug, plan_id=free_plan.id)
    session.add(org)
    await session.flush()  # populate org.id before referencing

    user = User(
        username=username,
        password_hash=hash_password(req.password),
        display_name=req.display_name.strip(),
        email=email,
        email_verified_at=None,
        signup_token=token,
        organization_id=org.id,
    )
    session.add(user)
    await session.flush()

    session.add(OrganizationMembership(
        org_id=org.id, user_id=user.id, role="owner",
    ))
    await session.commit()
    await session.refresh(user)

    # Dispatch the verification email. Failure does NOT roll back the
    # signup — the verify URL is logged so an operator can recover.
    sent = True
    try:
        from shared.email import send_verification_email

        await send_verification_email(email, token)
    except Exception:
        sent = False

    return SignupResponse(user_id=user.id, email=email, verification_sent=sent)


@router.get("/auth/verify/{token}")
async def verify_email(
    token: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark the user's email verified, clear the token, and set a session
    cookie so the click-through completes the signup in one step."""
    result = await session.execute(
        select(User).where(User.signup_token == token)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "Invalid or expired verification link")

    user.email_verified_at = datetime.now(UTC)
    user.signup_token = None
    user.last_login = datetime.now(UTC)
    org_id = await _resolve_active_org_id(session, user)
    await session.commit()

    jwt = create_token(
        user_id=user.id, username=user.username, current_org_id=org_id,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        path="/",
    )
    return {"ok": True, "user_id": user.id, "email": user.email}


@router.patch("/auth/me/email")
async def change_my_email(
    req: ChangeEmailRequest,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> dict:
    """Change the caller's email — re-issues a signup_token, clears
    ``email_verified_at``, and dispatches a new verification email."""
    import secrets as _stdlib_secrets

    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    new_email = _normalise_email(req.email)
    if not _EMAIL_RE.match(new_email):
        raise HTTPException(400, "Invalid email address")

    existing = await session.execute(
        select(User).where(User.email == new_email, User.id != payload["user_id"])
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Email already in use")

    token = _stdlib_secrets.token_urlsafe(32)
    await session.execute(
        sql_update(User)
        .where(User.id == payload["user_id"])
        .values(email=new_email, email_verified_at=None, signup_token=token)
    )
    await session.commit()

    sent = True
    try:
        from shared.email import send_verification_email

        await send_verification_email(new_email, token)
    except Exception:
        sent = False
    return {"ok": True, "email": new_email, "verification_sent": sent}


# --- Per-user secrets API ---


@router.get("/me/secrets")
async def list_my_secrets(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> SecretListResponse:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    from shared import secrets as _user_secrets

    keys = await _user_secrets.list_keys(
        payload["user_id"], org_id=payload["current_org_id"], session=session,
    )
    return SecretListResponse(keys=keys)


@router.put("/me/secrets/{key}")
async def put_my_secret(
    key: str,
    body: SecretPutRequest,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> dict:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    from shared import secrets as _user_secrets

    if key not in _user_secrets.SECRET_KEYS:
        raise HTTPException(404, "Unknown secret key")

    org_id = payload["current_org_id"]
    if body.value is None or body.value == "":
        await _user_secrets.delete(
            payload["user_id"], key, org_id=org_id, session=session,
        )
        await session.commit()
        return {"ok": True, "cleared": True}

    await _user_secrets.set(
        payload["user_id"], key, body.value, org_id=org_id, session=session,
    )
    await session.commit()
    return {"ok": True, "cleared": False}


@router.delete("/me/secrets/{key}", status_code=204)
async def delete_my_secret(
    key: str,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> Response:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    from shared import secrets as _user_secrets

    if key not in _user_secrets.SECRET_KEYS:
        raise HTTPException(404, "Unknown secret key")

    await _user_secrets.delete(
        payload["user_id"], key, org_id=payload["current_org_id"], session=session,
    )
    await session.commit()
    return Response(status_code=204)


@router.post("/me/secrets/{key}/test")
async def test_my_secret(
    key: str,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> SecretTestResponse:
    """Backend-validated connectivity test for the stored secret. Avoids
    sending the secret to the browser just to test it."""
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    from shared import secrets as _user_secrets

    if key not in _user_secrets.SECRET_KEYS:
        raise HTTPException(404, "Unknown secret key")

    value = await _user_secrets.get(
        payload["user_id"], key, org_id=payload["current_org_id"], session=session,
    )
    if not value:
        return SecretTestResponse(ok=False, detail="Not set")

    if key == "github_pat":
        import httpx

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {value}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if r.status_code == 200:
            login = r.json().get("login", "")
            return SecretTestResponse(ok=True, detail=f"Authenticated as {login}")
        return SecretTestResponse(
            ok=False, detail=f"GitHub returned {r.status_code}"
        )

    if key == "anthropic_api_key":
        import httpx

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages/count_tokens",
                headers={
                    "x-api-key": value,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        if r.status_code == 200:
            tok = r.json().get("input_tokens", "?")
            return SecretTestResponse(ok=True, detail=f"OK ({tok} tokens)")
        return SecretTestResponse(
            ok=False, detail=f"Anthropic returned {r.status_code}"
        )

    return SecretTestResponse(ok=False, detail="No test handler for this key")


@router.post("/auth/users")
async def create_user(
    req: CreateUserRequest,
    session: AsyncSession = Depends(get_session),
    authorization: str = Header(None),
):
    _verify_auth_header(authorization)
    existing = await session.execute(select(User).where(User.username == req.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return UserData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.get("/auth/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    authorization: str = Header(None),
):
    _verify_auth_header(authorization)
    result = await session.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [
        UserData(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            created_at=u.created_at.isoformat() if u.created_at else None,
            last_login=u.last_login.isoformat() if u.last_login else None,
        )
        for u in users
    ]


# --- Task endpoints ---


@router.post("/tasks", response_model=TaskData)
async def create_task(
    req: CreateTaskRequest,
    session: AsyncSession = Depends(get_session),
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> TaskData:
    _check_rate_limit()

    # Derive ownership + active org from the authenticated session when
    # present, so the frontend doesn't have to pass them. Webhook callers
    # (Slack/Telegram/Linear) construct Task rows directly without going
    # through this endpoint, so for those code paths org_id is supplied
    # by the caller via req.organization_id (or defaults to the legacy
    # behaviour: no scoping until migration 027 flips NOT NULL).
    authed_user_id: int | None = None
    caller_org_id: int | None = None
    if auto_agent_session or authorization:
        try:
            payload = _verify_cookie_or_header(auto_agent_session, authorization)
            authed_user_id = payload["user_id"]
            caller_org_id = payload.get("current_org_id")
        except HTTPException:
            authed_user_id = None
    owner_user_id = authed_user_id or req.created_by_user_id

    # Rate-limit task creation per org.
    if caller_org_id is not None:
        try:
            await quotas.enforce_task_create_limit(session, caller_org_id)
        except quotas.QuotaExceeded as e:
            raise HTTPException(
                status_code=429,
                detail=str(e),
                headers={"Retry-After": str(_seconds_until_utc_midnight())},
            ) from e

    # Dedup check: scoped to caller's org when known so two tenants
    # receiving the same Slack message ID don't dedupe each other.
    dup = await find_duplicate_by_source_id(
        session, req.source_id, organization_id=caller_org_id,
    )
    if not dup:
        dup = await find_duplicate_by_title(
            session, req.title, organization_id=caller_org_id,
        )
    if dup:
        return _task_to_response(dup)

    # Resolve repo — only repos in the caller's org are visible.
    repo = None
    if req.repo_name:
        if caller_org_id is not None:
            repo = await _get_repo_in_org(
                session, name=req.repo_name, org_id=caller_org_id,
            )
        else:
            result = await session.execute(
                select(Repo).where(Repo.name == req.repo_name)
            )
            repo = result.scalar_one_or_none()

    # Reject only if the user hasn't paired AND there's no fallback configured.
    # When a fallback is set (e.g. admin's user_id), unpaired users transparently
    # share the admin's Claude credentials.
    if owner_user_id is not None and settings.fallback_claude_user_id is None:
        user_q = await session.execute(
            select(User).where(User.id == owner_user_id)
        )
        user_row = user_q.scalar_one_or_none()
        if user_row is not None and user_row.claude_auth_status != "paired":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Connect your Claude account in Settings before queuing tasks."
                ),
            )

    task = Task(
        title=req.title,
        description=req.description,
        source=req.source,
        source_id=req.source_id,
        repo_id=repo.id if repo else None,
        created_by_user_id=owner_user_id,
        organization_id=caller_org_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Publish event
    await publish(task_created(task.id))

    return _task_to_response(task)


@router.get("/tasks", response_model=list[TaskData])
async def list_tasks(
    status: TaskStatus | None = None,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[TaskData]:
    query = scoped(select(Task), Task, org_id=org_id).order_by(
        Task.created_at.desc(),
    ).limit(50)
    if status:
        query = query.where(Task.status == status)
    result = await session.execute(query)
    return [_task_to_response(t) for t in result.scalars().all()]


@router.get("/tasks/{task_id}", response_model=TaskData)
async def get_task_detail(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return _task_to_response(task)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # Delete history first (FK constraint)
    await session.execute(select(TaskHistory).where(TaskHistory.task_id == task_id))
    await session.execute(sql_delete(TaskHistory).where(TaskHistory.task_id == task_id))
    await session.delete(task)
    await session.commit()

    # Publish cleanup event to free workspace
    await publish(task_cleanup(task_id))
    await publish(task_failed(task_id))

    return {"deleted": task_id}


class PriorityRequest(BaseModel):
    priority: int = Field(ge=0, le=999)


@router.post("/tasks/{task_id}/priority", response_model=TaskData)
async def set_task_priority(
    task_id: int,
    req: PriorityRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    """Set a task's queue priority. Lower number = picked up first.

    0 = jump to front, 100 = normal (default), 999 = lowest.
    Only affects QUEUED tasks — tasks already in progress are unaffected.
    """
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.priority = req.priority
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskData)
async def cancel_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        raise HTTPException(400, f"Task already in terminal state: {task.status.value}")
    # Force to failed regardless of current state
    task.status = TaskStatus.FAILED
    session.add(
        TaskHistory(
            task_id=task.id,
            from_status=task.status,
            to_status=TaskStatus.FAILED,
            message="Cancelled by user",
        )
    )
    await session.commit()

    await publish(task_cleanup(task_id))
    await publish(task_failed(task_id))

    return _task_to_response(task)


@router.post("/tasks/{task_id}/transition", response_model=TaskData)
async def transition_task(
    task_id: int,
    req: TransitionRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        # Save plan if provided (agent sends it when transitioning to awaiting_approval)
        if req.plan is not None:
            task.plan = req.plan
        task = await transition(session, task, req.status, req.message)
        await session.commit()
    except InvalidTransition as e:
        raise HTTPException(400, str(e))
    return _task_to_response(task)


class AssignRepoRequest(BaseModel):
    repo_name: str


@router.patch("/tasks/{task_id}/repo", response_model=TaskData)
async def assign_repo(
    task_id: int,
    req: AssignRepoRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    repo = await _get_repo_in_org(session, name=req.repo_name, org_id=org_id)
    if not repo:
        raise HTTPException(404, f"Repo '{req.repo_name}' not found")
    task.repo_id = repo.id
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


class BranchUpdate(BaseModel):
    branch_name: str


@router.patch("/tasks/{task_id}/branch", response_model=TaskData)
async def set_branch_name(
    task_id: int,
    req: BranchUpdate,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.branch_name = req.branch_name
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


class SubtaskUpdate(BaseModel):
    subtasks: list[dict]
    current_subtask: int | None = None


@router.patch("/tasks/{task_id}/subtasks", response_model=TaskData)
async def update_subtasks(
    task_id: int,
    req: SubtaskUpdate,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.subtasks = req.subtasks
    task.current_subtask = req.current_subtask
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


class IntakeQaUpdate(BaseModel):
    """Update the grill-before-planning Q&A on a task."""
    intake_qa: list[dict]


@router.patch("/tasks/{task_id}/intake_qa", response_model=TaskData)
async def update_intake_qa(
    task_id: int,
    req: IntakeQaUpdate,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    """Replace the task's grill Q&A list (used by the agent during the grill phase)."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.intake_qa = req.intake_qa
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


class AffectedRoutesUpdate(BaseModel):
    """Planner-declared routes affected by a task (set during planning phase)."""
    routes: list[dict] = Field(default_factory=list)


@router.post("/tasks/{task_id}/affected_routes", response_model=TaskData)
async def set_affected_routes(
    task_id: int,
    req: AffectedRoutesUpdate,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    """Replace the task's planner-declared affected routes."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.affected_routes = req.routes or []
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


def _verify_attempt_to_out(a: VerifyAttempt) -> VerifyAttemptOut:
    return VerifyAttemptOut(
        id=a.id,
        cycle=a.cycle,
        status=a.status,
        boot_check=a.boot_check,
        intent_check=a.intent_check,
        intent_judgment=a.intent_judgment,
        tool_calls=a.tool_calls,
        failure_reason=a.failure_reason,
        log_tail=a.log_tail,
        started_at=a.started_at,
        finished_at=a.finished_at,
    )


def _review_attempt_to_out(a: ReviewAttempt) -> ReviewAttemptOut:
    return ReviewAttemptOut(
        id=a.id,
        cycle=a.cycle,
        status=a.status,
        code_review_verdict=a.code_review_verdict,
        ui_check=a.ui_check,
        ui_judgment=a.ui_judgment,
        tool_calls=a.tool_calls,
        failure_reason=a.failure_reason,
        log_tail=a.log_tail,
        started_at=a.started_at,
        finished_at=a.finished_at,
    )


@router.get(
    "/tasks/{task_id}/verify-attempts",
    response_model=list[VerifyAttemptOut],
)
async def list_verify_attempts(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[VerifyAttemptOut]:
    """Return all verify attempts for a task, oldest cycle first."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    rows = (
        await session.execute(
            select(VerifyAttempt)
            .where(VerifyAttempt.task_id == task_id)
            .order_by(VerifyAttempt.cycle.asc()),
        )
    ).scalars().all()
    return [_verify_attempt_to_out(r) for r in rows]


@router.get(
    "/tasks/{task_id}/review-attempts",
    response_model=list[ReviewAttemptOut],
)
async def list_review_attempts(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[ReviewAttemptOut]:
    """Return all review attempts for a task, oldest cycle first."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    rows = (
        await session.execute(
            select(ReviewAttempt)
            .where(ReviewAttempt.task_id == task_id)
            .order_by(ReviewAttempt.cycle.asc()),
        )
    ).scalars().all()
    return [_review_attempt_to_out(r) for r in rows]


@router.get("/tasks/{task_id}/messages", response_model=list[TaskMessageData])
async def list_task_messages(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[TaskMessageData]:
    """Return all user-posted messages for a task, oldest first."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    result = await session.execute(
        select(TaskMessage)
        .where(TaskMessage.task_id == task_id)
        .order_by(TaskMessage.created_at.asc())
    )
    rows = result.scalars().all()
    return [
        TaskMessageData(
            id=m.id,
            task_id=m.task_id,
            sender=m.sender,
            content=m.content,
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in rows
    ]


@router.post("/tasks/{task_id}/messages", response_model=TaskMessageData)
async def post_task_message(
    task_id: int,
    req: TaskMessagePost,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
    authorization: str = Header(None),
    x_sender: str = Header(None),
) -> TaskMessageData:
    """Post a feedback message to a task.

    Sender resolution order:
    1. Authenticated user's display_name (if Authorization header present).
    2. `X-Sender` header (used by internal callers like the Telegram bridge).
    3. Falls back to "anonymous".
    """
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(400, "content must not be empty")

    sender = "anonymous"
    if authorization:
        try:
            payload = _verify_auth_header(authorization)
            user_result = await session.execute(
                select(User).where(User.id == payload["user_id"])
            )
            user = user_result.scalar_one_or_none()
            if user:
                sender = user.display_name
        except HTTPException:
            if x_sender:
                sender = x_sender
    elif x_sender:
        sender = x_sender

    msg = TaskMessage(task_id=task_id, sender=sender, content=content)
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    # Push onto the agent's guidance queue so the loop picks it up on its
    # next turn, and publish an event for any other subscribers.
    formatted = f"{sender}: {content}" if sender not in ("anonymous",) else content
    await task_channel(task_id).push_guidance(formatted)
    await publish(task_feedback(task_id=task_id, message_id=msg.id, sender=sender))

    return TaskMessageData(
        id=msg.id,
        task_id=msg.task_id,
        sender=msg.sender,
        content=msg.content,
        created_at=msg.created_at.isoformat() if msg.created_at else None,
    )


@router.post("/tasks/{task_id}/done", response_model=TaskData)
async def mark_task_done(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status == TaskStatus.DONE:
        return _task_to_response(task)

    # Allow marking done from any state that has DONE as a valid transition
    from orchestrator.state_machine import TRANSITIONS
    if TaskStatus.DONE not in TRANSITIONS.get(task.status, set()):
        raise HTTPException(400, f"Cannot mark done from {task.status.value}")

    if task.status == TaskStatus.AWAITING_REVIEW:
        await publish(task_review_approved(task.id))

    task = await transition(session, task, TaskStatus.DONE, "Marked done by user")
    await session.commit()
    return _task_to_response(task)


class TaskMessageRequest(BaseModel):
    message: str = Field(max_length=5000)
    username: str = Field(max_length=255)


@router.post("/tasks/{task_id}/message")
async def add_task_message(
    task_id: int,
    req: TaskMessageRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Persist a human message in the task's history."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    session.add(
        TaskHistory(
            task_id=task.id,
            from_status=task.status,
            to_status=task.status,  # No transition — just a message log
            message=f"[{req.username}] {req.message}",
        )
    )
    await session.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/approve", response_model=TaskData)
async def approve_task(
    task_id: int,
    req: ApprovalRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(400, f"Task is in {task.status.value}, not awaiting_approval")

    if req.approved:
        approve_msg = req.message or "Plan approved by user"
        task = await transition(session, task, TaskStatus.CODING, approve_msg)
        await session.commit()
        await publish(task_approved(task.id))
    else:
        # Clear the old plan and re-run planning with feedback
        task.plan = None
        reject_msg = req.message or f"Plan rejected: {req.feedback}"
        task = await transition(session, task, TaskStatus.PLANNING, reject_msg)
        await session.commit()
        await publish(task_rejected(task.id, feedback=req.feedback))
        await publish(task_start_planning(task.id, feedback=req.feedback))

    return _task_to_response(task)


@router.post("/tasks/{task_id}/pause-trio")
async def pause_trio(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Pause a running trio by transitioning the parent task to BLOCKED.

    Only valid when the task is in TRIO_EXECUTING status.
    Clears trio_phase so the trio can be resumed cleanly later.
    """
    task = await _get_task_in_org(session, task_id, org_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != TaskStatus.TRIO_EXECUTING:
        raise HTTPException(status_code=400, detail="task is not in TRIO_EXECUTING")
    task.trio_phase = None
    await transition(session, task, TaskStatus.BLOCKED)
    await session.commit()
    return {"ok": True}


# --- Feedback/Learning endpoints ---


@router.post("/tasks/{task_id}/outcome", response_model=OutcomeResponse)
async def record_task_outcome(
    task_id: int,
    req: RecordOutcomeRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> OutcomeResponse:
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    outcome = await record_outcome(
        session, task_id, req.pr_approved, req.review_rounds, req.feedback_summary
    )
    await session.commit()
    return OutcomeResponse(
        task_id=task_id,
        pr_approved=outcome.pr_approved,
        review_rounds=outcome.review_rounds,
    )


@router.get("/usage/summary", response_model=UsageSummary)
async def get_usage_summary(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> UsageSummary:
    """Today's usage for the caller's current org + the plan caps."""
    plan = await quotas.get_plan_for_org(session, org_id)
    active = await quotas.count_active_tasks_for_org(session, org_id)
    today_n = await quotas.count_tasks_created_today(session, org_id)
    in_tok, out_tok = await quotas.sum_tokens_today(session, org_id)
    return UsageSummary(
        plan=PlanRead.model_validate(plan, from_attributes=True),
        active_tasks=active,
        tasks_today=today_n,
        input_tokens_today=in_tok,
        output_tokens_today=out_tok,
    )


@router.get("/feedback/summary", response_model=FeedbackSummary)
async def feedback_summary(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> FeedbackSummary:
    return await get_feedback_summary(session, organization_id=org_id)


@router.get("/feedback/patterns", response_model=PatternsResponse)
async def feedback_patterns(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> PatternsResponse:
    analysis = await analyze_patterns(session, organization_id=org_id)
    return PatternsResponse(analysis=analysis)


# --- Schedule endpoints ---


@router.post("/schedules", response_model=ScheduleResponse)
async def create_schedule(
    req: CreateScheduleRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> ScheduleResponse:
    schedule = ScheduledTask(
        name=req.name,
        cron_expression=req.cron_expression,
        task_title=req.task_title,
        task_description=req.task_description,
        repo_name=req.repo_name,
        organization_id=org_id,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return _schedule_to_response(schedule)


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[ScheduleResponse]:
    result = await session.execute(
        scoped(select(ScheduledTask), ScheduledTask, org_id=org_id).order_by(
            ScheduledTask.name,
        )
    )
    return [_schedule_to_response(s) for s in result.scalars().all()]


@router.delete("/schedules/{schedule_id}", response_model=DeleteResponse)
async def delete_schedule(
    schedule_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> DeleteResponse:
    result = await session.execute(
        scoped(
            select(ScheduledTask).where(ScheduledTask.id == schedule_id),
            ScheduledTask, org_id=org_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    await session.delete(schedule)
    await session.commit()
    return DeleteResponse(deleted=schedule_id)


@router.post("/schedules/{schedule_id}/toggle", response_model=ToggleResponse)
async def toggle_schedule(
    schedule_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> ToggleResponse:
    result = await session.execute(
        scoped(
            select(ScheduledTask).where(ScheduledTask.id == schedule_id),
            ScheduledTask, org_id=org_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    schedule.enabled = not schedule.enabled
    await session.commit()
    return ToggleResponse(id=schedule.id, enabled=schedule.enabled)


# --- Repo endpoints ---


@router.post("/repos", response_model=RepoResponse)
async def register_repo(
    req: RegisterRepoRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> RepoResponse:
    repo = Repo(
        name=req.name, url=req.url, default_branch=req.default_branch,
        organization_id=org_id,
    )
    session.add(repo)
    await session.commit()
    await session.refresh(repo)
    return RepoResponse(id=repo.id, name=repo.name, url=repo.url)


@router.get("/repos", response_model=list[RepoData])
async def list_repos(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[RepoData]:
    result = await session.execute(
        scoped(select(Repo), Repo, org_id=org_id).order_by(Repo.name)
    )
    return [
        RepoData(
            id=r.id,
            name=r.name,
            url=r.url,
            default_branch=r.default_branch,
            summary=r.summary,
            summary_updated_at=r.summary_updated_at.isoformat() if r.summary_updated_at else None,
            ci_checks=r.ci_checks,
            harness_onboarded=r.harness_onboarded or False,
            harness_pr_url=r.harness_pr_url,
        )
        for r in result.scalars().all()
    ]


@router.patch("/repos/{repo_name}/branch")
async def update_repo_branch(
    repo_name: str,
    req: dict,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Update a repo's default branch. Updates all entries matching the name
    (both short name like 'cardamon' and full name like 'org/cardamon').
    Body: {"default_branch": "prod"}
    """
    new_branch = req.get("default_branch", "").strip()
    if not new_branch:
        raise HTTPException(400, "default_branch is required")
    if not BRANCH_NAME_RE.match(new_branch):
        raise HTTPException(
            400, "Invalid branch name: only alphanumeric, '.', '_', '/', '-' allowed"
        )

    # Find all repo entries that match (short name, full name with org/), scoped to caller's org
    result = await session.execute(
        scoped(
            select(Repo).where(
                (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
            ),
            Repo, org_id=org_id,
        )
    )
    repos = result.scalars().all()
    if not repos:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    old_branch = repos[0].default_branch
    updated = []
    for repo in repos:
        repo.default_branch = new_branch
        updated.append(repo.name)
    await session.commit()

    return {
        "repo": repo_name,
        "old_branch": old_branch,
        "new_branch": new_branch,
        "updated": updated,
    }


@router.post("/repos/{repo_name}/refresh-ci")
async def refresh_repo_ci_checks(
    repo_name: str,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Re-extract CI checks from a repo's workflow files."""
    from orchestrator.ci_extractor import extract_ci_checks

    result = await session.execute(
        scoped(
            select(Repo).where(
                (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
            ),
            Repo, org_id=org_id,
        )
    )
    repos = result.scalars().all()
    if not repos:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    ci_checks = await extract_ci_checks(repos[0].url)
    for repo in repos:
        repo.ci_checks = ci_checks
    await session.commit()

    return {"repo": repo_name, "ci_checks": ci_checks}


@router.post("/repos/{repo_id}/harness")
async def update_repo_harness(
    repo_id: int,
    req: dict,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Mark a repo as harness-onboarded and store the PR URL."""
    repo = await _get_repo_in_org(session, repo_id=repo_id, org_id=org_id)
    if not repo:
        raise HTTPException(404, "Repo not found")
    repo.harness_onboarded = req.get("harness_onboarded", False)
    repo.harness_pr_url = req.get("harness_pr_url")
    await session.commit()
    return {"ok": True}


@router.post("/repos/{repo_name}/onboard")
async def trigger_harness_onboarding(
    repo_name: str,
    force: bool = False,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Trigger harness engineering onboarding for a repo. Returns immediately.

    Pass ?force=true to re-onboard a repo that was already onboarded.
    """
    result = await session.execute(
        scoped(
            select(Repo).where(
                (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
            ),
            Repo, org_id=org_id,
        )
    )
    repo = result.scalars().first()
    if not repo:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    if repo.harness_onboarded and not force:
        return {"status": "already_onboarded", "pr_url": repo.harness_pr_url}

    # Reset status so onboarding runs fresh
    if force and repo.harness_onboarded:
        repo.harness_onboarded = False
        repo.harness_pr_url = None
        await session.commit()

    # Publish event to trigger onboarding asynchronously
    await publish(repo_onboard(repo_id=repo.id, repo_name=repo.name))

    return {"status": "onboarding_started", "repo": repo.name}


@router.get("/tasks/{task_id}/history")
async def get_task_history(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[dict]:
    # Scope by joining through the parent Task to enforce org boundary.
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    result = await session.execute(
        select(TaskHistory)
        .where(TaskHistory.task_id == task_id)
        .order_by(TaskHistory.created_at.asc())
    )
    return [
        {
            "from_status": h.from_status.value if h.from_status else None,
            "to_status": h.to_status.value,
            "message": h.message,
            "timestamp": h.created_at.isoformat() if h.created_at else None,
        }
        for h in result.scalars().all()
    ]


@router.post("/repos/{repo_id}/summary")
async def update_repo_summary(
    repo_id: int,
    req: dict,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    repo = await _get_repo_in_org(session, repo_id=repo_id, org_id=org_id)
    if not repo:
        raise HTTPException(404, "Repo not found")
    repo.summary = req.get("summary", "")
    repo.summary_updated_at = datetime.now(UTC)
    await session.commit()
    return {"ok": True}


@router.get("/repos/{repo_id}/market-brief/latest", response_model=MarketBriefResponse)
async def get_latest_market_brief(
    repo_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Return the most recent MarketBrief for a repo, or 404 if none exists."""
    result = await session.execute(
        select(MarketBrief)
        .where(MarketBrief.repo_id == repo_id)
        .where(MarketBrief.organization_id == org_id)
        .order_by(MarketBrief.created_at.desc())
        .limit(1)
    )
    brief = result.scalar_one_or_none()
    if brief is None:
        raise HTTPException(404, "No market brief found for this repo")
    return {
        "id": brief.id,
        "repo_id": brief.repo_id,
        "created_at": brief.created_at.isoformat(),
        "product_category": brief.product_category,
        "competitors": brief.competitors,
        "findings": brief.findings,
        "modality_gaps": brief.modality_gaps,
        "strategic_themes": brief.strategic_themes,
        "summary": brief.summary,
        "partial": brief.partial,
    }


@router.delete("/repos/{repo_name}")
async def delete_repo(
    repo_name: str,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Remove a repo and its associated FreeformConfig + pending suggestions."""
    repo = await _get_repo_in_org(session, name=repo_name, org_id=org_id)
    if not repo:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    # Block deletion if any tasks are still active
    active_result = await session.execute(
        select(Task).where(
            Task.repo_id == repo.id,
            Task.status.notin_(TERMINAL_STATUSES),
        )
    )
    active_tasks = active_result.scalars().all()
    if active_tasks:
        raise HTTPException(
            409,
            f"Cannot delete repo '{repo_name}': {len(active_tasks)} active task(s) reference it",
        )

    # Delete all suggestions — including approved/rejected ones — because
    # Suggestion.repo_id is non-nullable, so we can't orphan them.
    await session.execute(sql_delete(Suggestion).where(Suggestion.repo_id == repo.id))
    # Cascade-delete freeform config
    await session.execute(sql_delete(FreeformConfig).where(FreeformConfig.repo_id == repo.id))
    # Orphan completed/failed tasks (don't delete them)
    await session.execute(sql_update(Task).where(Task.repo_id == repo.id).values(repo_id=None))

    await session.delete(repo)
    await session.commit()

    # Publish event so background loops can react
    await publish(repo_deleted(repo_name=repo_name))

    return {"deleted": repo_name}


# --- Freeform / Suggestions ---


class FreeformConfigRequest(BaseModel):
    repo_name: str = Field(max_length=256)
    # `prod_branch` is optional — if omitted, server fills it with the repo's
    # default_branch. This lets existing clients keep working unchanged.
    prod_branch: str | None = Field(default=None, max_length=256)
    dev_branch: str = Field(default="dev", max_length=256)
    analysis_cron: str = Field(default="0 9 * * 1", max_length=128)
    enabled: bool = True
    auto_approve_suggestions: bool = False
    auto_start_tasks: bool = False
    po_goal: str | None = Field(default=None, max_length=4000)
    # Architecture Mode — periodic improve-codebase-architecture cron
    # producing deepening Suggestions. Off by default so existing repos are
    # unaffected.
    architecture_mode: bool = False
    architecture_cron: str = Field(default="0 9 * * 1", max_length=128)

    @field_validator("prod_branch", "dev_branch")
    @classmethod
    def validate_branch(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not BRANCH_NAME_RE.match(v):
            raise ValueError(
                "Invalid branch name: only alphanumeric, '.', '_', '/', '-' allowed"
            )
        return v

    @field_validator("analysis_cron", "architecture_cron")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError("Invalid cron expression")
        return v


@router.post("/freeform/config", response_model=FreeformConfigData)
async def upsert_freeform_config(
    req: FreeformConfigRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> FreeformConfigData:
    """Enable or update freeform mode for a repo."""
    repo = await _get_repo_in_org(session, name=req.repo_name, org_id=org_id)
    if not repo:
        raise HTTPException(404, f"Repo '{req.repo_name}' not found")

    # Default prod_branch to the repo's default branch if caller didn't specify
    prod_branch = req.prod_branch or repo.default_branch or "main"

    result = await session.execute(
        scoped(
            select(FreeformConfig).where(FreeformConfig.repo_id == repo.id),
            FreeformConfig, org_id=org_id,
        )
    )
    config = result.scalar_one_or_none()
    if config:
        config.enabled = req.enabled
        config.prod_branch = prod_branch
        config.dev_branch = req.dev_branch
        config.analysis_cron = req.analysis_cron
        config.auto_approve_suggestions = req.auto_approve_suggestions
        config.auto_start_tasks = req.auto_start_tasks
        config.po_goal = req.po_goal
        config.architecture_mode = req.architecture_mode
        config.architecture_cron = req.architecture_cron
    else:
        config = FreeformConfig(
            repo_id=repo.id,
            enabled=req.enabled,
            prod_branch=prod_branch,
            dev_branch=req.dev_branch,
            analysis_cron=req.analysis_cron,
            auto_approve_suggestions=req.auto_approve_suggestions,
            auto_start_tasks=req.auto_start_tasks,
            po_goal=req.po_goal,
            architecture_mode=req.architecture_mode,
            architecture_cron=req.architecture_cron,
            organization_id=org_id,
        )
        session.add(config)
    await session.commit()
    await session.refresh(config)
    return _freeform_config_to_response(config, repo.name)


@router.get("/freeform/config", response_model=list[FreeformConfigData])
async def list_freeform_configs(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[FreeformConfigData]:
    result = await session.execute(
        scoped(select(FreeformConfig), FreeformConfig, org_id=org_id)
    )
    configs = result.scalars().all()
    out = []
    for c in configs:
        repo_result = await session.execute(
            scoped(
                select(Repo).where(Repo.id == c.repo_id), Repo, org_id=org_id,
            )
        )
        repo = repo_result.scalar_one_or_none()
        out.append(_freeform_config_to_response(c, repo.name if repo else None))
    return out


@router.delete("/freeform/config/{config_id}")
async def delete_freeform_config(
    config_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    result = await session.execute(
        scoped(
            select(FreeformConfig).where(FreeformConfig.id == config_id),
            FreeformConfig, org_id=org_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Config not found")
    await session.delete(config)
    await session.commit()
    return {"ok": True}


class CreateRepoRequest(BaseModel):
    description: str
    org: str = ""
    private: bool = True
    # When True (default), the new repo enters the continuous-improvement loop
    # immediately: every-30-min PO analysis with auto-approval of suggestions.
    loop: bool = True


@router.post("/freeform/create-repo")
async def create_repo_from_description(
    req: CreateRepoRequest,
    session: AsyncSession = Depends(get_session),
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> dict:
    """Create a brand-new GitHub repo from a natural-language description.

    Picks a name via Claude, creates the repo on GitHub (private, auto_init),
    enables freeform mode for it, and queues a scaffold task that runs through
    the normal coding pipeline with auto-approval.
    """
    from orchestrator.create_repo import CreateRepoError, create_repo_and_scaffold_task

    if not req.description.strip():
        raise HTTPException(400, "description is required")

    user_id: int | None = None
    caller_org_id: int | None = None
    if auto_agent_session or authorization:
        try:
            payload = _verify_cookie_or_header(auto_agent_session, authorization)
            user_id = payload.get("user_id")
            caller_org_id = payload.get("current_org_id")
        except HTTPException:
            user_id = None

    try:
        repo, task = await create_repo_and_scaffold_task(
            session,
            description=req.description,
            org_override=req.org,
            private=req.private,
            loop=req.loop,
            user_id=user_id,
            organization_id=caller_org_id,
        )
    except CreateRepoError as e:
        raise HTTPException(400, str(e))

    return {
        "repo": {
            "id": repo.id,
            "name": repo.name,
            "url": repo.url,
            "default_branch": repo.default_branch,
        },
        "task": _task_to_response(task).model_dump(),
    }


@router.post("/freeform/analyze/{repo_name}")
async def trigger_po_analysis(
    repo_name: str,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Manually trigger a PO analysis for a repo."""
    repo = await _get_repo_in_org(session, name=repo_name, org_id=org_id)
    if not repo:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    await publish(po_analyze(repo_id=repo.id, repo_name=repo.name))
    return {"ok": True, "message": f"PO analysis triggered for {repo_name}"}


@router.get("/suggestions", response_model=list[SuggestionData])
async def list_suggestions(
    status: str | None = None,
    repo_name: str | None = None,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> list[SuggestionData]:
    query = scoped(select(Suggestion), Suggestion, org_id=org_id).order_by(
        Suggestion.created_at.desc(),
    ).limit(100)
    if status:
        query = query.where(Suggestion.status == status)
    if repo_name:
        repo = await _get_repo_in_org(session, name=repo_name, org_id=org_id)
        if repo:
            query = query.where(Suggestion.repo_id == repo.id)
    result = await session.execute(query)
    suggestions = result.scalars().all()
    return [await _suggestion_to_response(session, s) for s in suggestions]


@router.post("/suggestions/{suggestion_id}/approve", response_model=TaskData)
async def approve_suggestion(
    suggestion_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> TaskData:
    """Approve a suggestion — creates a freeform task."""
    result = await session.execute(
        scoped(
            select(Suggestion).where(Suggestion.id == suggestion_id),
            Suggestion, org_id=org_id,
        )
    )
    suggestion = result.scalar_one_or_none()
    if not suggestion:
        raise HTTPException(404, "Suggestion not found")
    if suggestion.status != SuggestionStatus.PENDING:
        raise HTTPException(400, f"Suggestion is already {suggestion.status.value}")

    # Create task from suggestion. intake_qa_for_suggestion routes
    # pre-grilled categories (e.g. architecture) to [] to skip the grill
    # phase; everything else stays None to grill normally.
    task = Task(
        title=suggestion.title,
        description=suggestion.description,
        source=TaskSource.FREEFORM,
        source_id=f"suggestion:{suggestion.id}",
        repo_id=suggestion.repo_id,
        freeform_mode=True,
        intake_qa=intake_qa_for_suggestion(suggestion.category),
        organization_id=org_id,
    )
    session.add(task)
    await session.flush()

    suggestion.status = SuggestionStatus.APPROVED
    suggestion.task_id = task.id
    await session.commit()

    # Trigger task pipeline
    await publish(task_created(task.id))

    return _task_to_response(task)


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    result = await session.execute(
        scoped(
            select(Suggestion).where(Suggestion.id == suggestion_id),
            Suggestion, org_id=org_id,
        )
    )
    suggestion = result.scalar_one_or_none()
    if not suggestion:
        raise HTTPException(404, "Suggestion not found")
    suggestion.status = SuggestionStatus.REJECTED
    await session.commit()
    return {"ok": True}


@router.post("/freeform/{task_id}/promote")
async def promote_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Promote a completed freeform task's changes from dev to main."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.freeform_mode:
        raise HTTPException(400, "Task is not a freeform task")
    if task.status != TaskStatus.DONE:
        raise HTTPException(400, f"Task is in {task.status.value}, not done")
    if not task.pr_url:
        raise HTTPException(400, "Task has no PR URL")

    branch_name = task.branch_name or f"auto-agent/task-{task_id}"
    repo_url = task.repo.url if task.repo else ""
    pr_url = await promote_task_to_main(
        task.pr_url, branch_name, repo_url, user_id=task.created_by_user_id,
    )
    if not pr_url:
        raise HTTPException(500, "Failed to create promotion PR")
    return {"ok": True, "pr_url": pr_url}


@router.post("/freeform/{task_id}/revert")
async def revert_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Revert a freeform task's changes from the dev branch."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.freeform_mode:
        raise HTTPException(400, "Task is not a freeform task")
    if not task.pr_url:
        raise HTTPException(400, "Task has no PR URL")

    # Get the dev branch from freeform config
    dev_branch = "dev"
    if task.repo_id:
        result = await session.execute(
            select(FreeformConfig).where(FreeformConfig.repo_id == task.repo_id)
        )
        config = result.scalar_one_or_none()
        if config:
            dev_branch = config.dev_branch or "dev"

    revert_url = await revert_task_from_dev(
        task.pr_url, dev_branch, user_id=task.created_by_user_id,
    )
    if not revert_url:
        raise HTTPException(500, "Failed to create revert PR")
    return {"ok": True, "pr_url": revert_url}


# --- Helpers ---


async def _get_repo_by_name(session: AsyncSession, name: str) -> Repo | None:
    result = await session.execute(select(Repo).where(Repo.name == name))
    return result.scalar_one_or_none()


async def _suggestion_to_response(session: AsyncSession, s: Suggestion) -> SuggestionData:
    repo_result = await session.execute(select(Repo).where(Repo.id == s.repo_id))
    repo = repo_result.scalar_one_or_none()
    return SuggestionData(
        id=s.id,
        repo_id=s.repo_id,
        repo_name=repo.name if repo else None,
        title=s.title,
        description=s.description,
        rationale=s.rationale,
        category=s.category or "",
        priority=s.priority or 3,
        status=s.status.value if s.status else "pending",
        task_id=s.task_id,
        created_at=s.created_at.isoformat() if s.created_at else None,
        evidence_urls=s.evidence_urls or [],
    )


def _freeform_config_to_response(c: FreeformConfig, repo_name: str | None) -> FreeformConfigData:
    return FreeformConfigData(
        id=c.id,
        repo_name=repo_name,
        enabled=c.enabled or False,
        prod_branch=c.prod_branch or "main",
        dev_branch=c.dev_branch or "dev",
        analysis_cron=c.analysis_cron or "0 9 * * 1",
        auto_approve_suggestions=c.auto_approve_suggestions or False,
        auto_start_tasks=c.auto_start_tasks or False,
        po_goal=c.po_goal,
        last_analysis_at=c.last_analysis_at.isoformat() if c.last_analysis_at else None,
        architecture_mode=c.architecture_mode or False,
        architecture_cron=c.architecture_cron or "0 9 * * 1",
        last_architecture_at=(
            c.last_architecture_at.isoformat() if c.last_architecture_at else None
        ),
        architecture_knowledge=c.architecture_knowledge,
        created_at=c.created_at.isoformat() if c.created_at else None,
    )


def _task_to_response(task: Task) -> TaskData:
    return TaskData(
        id=task.id,
        title=task.title,
        description=task.description,
        source=task.source.value,
        status=task.status.value,
        complexity=task.complexity.value if task.complexity else None,
        repo_name=task.repo.name if task.repo else None,
        branch_name=task.branch_name,
        pr_url=task.pr_url,
        plan=task.plan,
        error=task.error,
        freeform_mode=task.freeform_mode or False,
        priority=task.priority if task.priority is not None else 100,
        subtasks=task.subtasks,
        current_subtask=task.current_subtask,
        intake_qa=task.intake_qa,
        created_at=task.created_at.isoformat() if task.created_at else None,
        created_by_user_id=task.created_by_user_id,
        organization_id=task.organization_id,
        parent_task_id=task.parent_task_id,
    )


def _schedule_to_response(s: ScheduledTask) -> ScheduleResponse:
    return ScheduleResponse(
        id=s.id,
        name=s.name,
        cron=s.cron_expression,
        task_title=s.task_title,
        enabled=s.enabled,
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
    )


async def seed_admin_user() -> None:
    """Create the admin user if no users exist."""
    import structlog

    from shared.config import settings
    from shared.database import async_session

    log = structlog.get_logger()

    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            if not settings.admin_password:
                log.warning(
                    "No admin_password set and no users exist — set ADMIN_PASSWORD env var"
                )
                return
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                display_name=settings.admin_username.title(),
            )
            session.add(admin)
            await session.commit()
            log.info("admin_user_created", username=settings.admin_username)


# ---------------------------------------------------------------------------
# Claude pairing
# ---------------------------------------------------------------------------


import structlog as _structlog

from orchestrator import claude_pairing as _claude_pairing
from orchestrator.claude_auth import vault_dir_for as _vault_dir_for
from shared.database import async_session as _async_session
from shared.events import Event as _Event

_pair_log = _structlog.get_logger("claude_pair")


class _PairStartResponse(BaseModel):
    pairing_id: str
    authorize_url: str


class _PairCodeBody(BaseModel):
    pairing_id: str
    code: str


class _PairStatusResponse(BaseModel):
    claude_auth_status: str
    claude_paired_at: datetime | None


@router.post("/claude/pair/start", response_model=_PairStartResponse)
async def claude_pair_start(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> _PairStartResponse:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    sess = await _claude_pairing.start_pairing(user_id=payload["user_id"])
    return _PairStartResponse(
        pairing_id=sess.pairing_id, authorize_url=sess.authorize_url
    )


@router.post("/claude/pair/code")
async def claude_pair_code(
    body: _PairCodeBody,
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> dict:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    sess = _claude_pairing.get_pairing(body.pairing_id)
    if sess is None or sess.user_id != payload["user_id"]:
        raise HTTPException(status_code=404, detail="pairing session not found")
    _pair_log.info(
        "claude_pair_code: submitting",
        pairing_id=body.pairing_id,
        user_id=payload["user_id"],
    )
    result = await _claude_pairing.complete_pairing(body.pairing_id, body.code)
    _pair_log.info(
        "claude_pair_code: result",
        success=result.success,
        exit_code=result.exit_code,
        stderr=result.stderr[:500],
    )
    if not result.success:
        raise HTTPException(status_code=400, detail=result.stderr or "pairing failed")

    try:
        async with _async_session() as s:
            await s.execute(
                sql_update(User)
                .where(User.id == payload["user_id"])
                .values(
                    claude_auth_status="paired",
                    claude_paired_at=datetime.now(UTC),
                )
            )
            await s.execute(
                sql_update(Task)
                .where(
                    Task.created_by_user_id == payload["user_id"],
                    Task.status == TaskStatus.BLOCKED_ON_AUTH,
                )
                .values(status=TaskStatus.QUEUED)
            )
            await s.commit()
    except Exception as e:
        _pair_log.exception("claude_pair_code: db update failed", error=str(e))
        # Tokens already on disk — return success to the client and surface
        # the persistence error in the logs.
        return {"ok": True, "warning": f"db update failed: {e}"}

    try:
        await publish(_Event(type="claude_pair_succeeded", task_id=None))
    except Exception as e:
        _pair_log.warning("claude_pair_code: publish failed", error=str(e))

    return {"ok": True}


@router.get("/claude/pair/status", response_model=_PairStatusResponse)
async def claude_pair_status(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> _PairStatusResponse:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    async with _async_session() as s:
        result = await s.execute(select(User).where(User.id == payload["user_id"]))
        user = result.scalar_one()
    return _PairStatusResponse(
        claude_auth_status=user.claude_auth_status,
        claude_paired_at=user.claude_paired_at,
    )


@router.post("/claude/pair/disconnect")
async def claude_pair_disconnect(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> dict:
    import shutil

    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    vault = _vault_dir_for(payload["user_id"])
    claude_subdir = os.path.join(vault, ".claude")
    if os.path.isdir(claude_subdir):
        shutil.rmtree(claude_subdir)
    async with _async_session() as s:
        await s.execute(
            sql_update(User)
            .where(User.id == payload["user_id"])
            .values(claude_auth_status="never_paired", claude_paired_at=None)
        )
        await s.commit()
    return {"ok": True}
