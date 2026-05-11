"""Organization member management + active-org switcher.

Lives in a dedicated module rather than ``orchestrator/router.py`` so the
router stays under its ~2000-line ceiling. Mounted by ``run.py`` via
``app.include_router(orgs.router, prefix="/api")``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.auth import (
    COOKIE_NAME,
    create_token,
    current_org_id as current_org_id_dep,
    current_user_id,
)
from shared.database import get_session
from shared.models import Organization, OrganizationMembership, User

router = APIRouter()

_ROLES_INVITABLE = {"admin", "member"}  # owner is reserved for the creator


class OrgOut(BaseModel):
    id: int
    name: str
    slug: str
    role: str


class MyOrgsResponse(BaseModel):
    orgs: list[OrgOut]
    current: OrgOut


class SwitchOrgRequest(BaseModel):
    org_id: int


class InviteRequest(BaseModel):
    email: str
    role: str = "member"


class RoleChangeRequest(BaseModel):
    role: str


class MemberOut(BaseModel):
    id: int
    username: str
    display_name: str
    email: str | None
    role: str
    joined_at: str


class MembersResponse(BaseModel):
    members: list[MemberOut]


# --- helpers -----------------------------------------------------------------


async def _membership(
    session: AsyncSession, *, user_id: int, org_id: int,
) -> OrganizationMembership | None:
    result = await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def _require_admin(membership: OrganizationMembership | None) -> None:
    if membership is None or membership.role not in ("owner", "admin"):
        # Use 404 instead of 403 to avoid leaking org existence.
        raise HTTPException(404, "Org not found")


def _require_owner(membership: OrganizationMembership | None) -> None:
    if membership is None or membership.role != "owner":
        raise HTTPException(403, "Only the owner can do that")


# --- endpoints ---------------------------------------------------------------


@router.get("/orgs/me", response_model=MyOrgsResponse)
async def list_my_orgs(
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
    session: AsyncSession = Depends(get_session),
) -> MyOrgsResponse:
    """Return every org the caller belongs to plus the active one."""
    rows = (await session.execute(
        select(Organization, OrganizationMembership.role)
        .join(
            OrganizationMembership,
            OrganizationMembership.org_id == Organization.id,
        )
        .where(OrganizationMembership.user_id == user_id)
        .order_by(Organization.created_at.asc())
    )).all()
    orgs = [
        OrgOut(id=o.id, name=o.name, slug=o.slug, role=role)
        for o, role in rows
    ]
    if not orgs:
        raise HTTPException(403, "User has no organization memberships")
    current = next((o for o in orgs if o.id == org_id), orgs[0])
    return MyOrgsResponse(orgs=orgs, current=current)


@router.post("/me/current-org")
async def switch_current_org(
    payload: SwitchOrgRequest,
    response: Response,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Re-issue the session cookie with a different ``current_org_id``.

    The caller MUST already be a member of the target org. Returns 403
    otherwise — this is the rare endpoint where 403 is preferred over
    404 because the org-switcher UI surface is the caller's own list,
    so existence isn't a secret here.
    """
    membership = await _membership(
        session, user_id=user_id, org_id=payload.org_id,
    )
    if membership is None:
        raise HTTPException(403, "Not a member of that organization")

    user = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one()
    new_token = create_token(
        user.id, user.username, current_org_id=payload.org_id,
    )
    response.set_cookie(
        COOKIE_NAME, new_token, httponly=True, samesite="lax", path="/",
    )
    membership.last_active_at = datetime.now(UTC)
    await session.commit()
    return {"current_org_id": payload.org_id}


@router.get("/orgs/{target_org_id}/members", response_model=MembersResponse)
async def list_members(
    target_org_id: int,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
    session: AsyncSession = Depends(get_session),
) -> MembersResponse:
    """List members of the caller's active org.

    Returns 404 if ``target_org_id`` doesn't match the caller's active
    org. We don't support listing members of an org you're not currently
    operating as — switch first.
    """
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = await _membership(session, user_id=user_id, org_id=org_id)
    if me is None:
        raise HTTPException(404, "Org not found")

    rows = (await session.execute(
        select(User, OrganizationMembership.role, OrganizationMembership.created_at)
        .join(
            OrganizationMembership,
            OrganizationMembership.user_id == User.id,
        )
        .where(OrganizationMembership.org_id == org_id)
        .order_by(OrganizationMembership.created_at.asc())
    )).all()
    return MembersResponse(members=[
        MemberOut(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            email=u.email,
            role=role,
            joined_at=joined.isoformat() if joined else "",
        )
        for u, role, joined in rows
    ])


@router.post("/orgs/{target_org_id}/members", status_code=201)
async def invite_member(
    target_org_id: int,
    payload: InviteRequest,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Add an existing verified user to the caller's active org.

    Cross-org invitations for non-existent users (the full "I'll create
    an account for you" flow) are deferred to a later phase — they need
    a separate email click-through workflow. For now: 404 if the email
    doesn't match a known user.
    """
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = await _membership(session, user_id=user_id, org_id=org_id)
    _require_admin(me)

    if payload.role not in _ROLES_INVITABLE:
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    email = payload.email.strip().lower()
    target = (await session.execute(
        select(User).where(User.email == email)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            404,
            "no_such_user — ask them to sign up at /signup first",
        )

    existing = await _membership(session, user_id=target.id, org_id=org_id)
    if existing is not None:
        raise HTTPException(409, "Already a member")

    session.add(OrganizationMembership(
        org_id=org_id, user_id=target.id, role=payload.role,
    ))
    await session.commit()
    return {"user_id": target.id, "role": payload.role}


@router.delete("/orgs/{target_org_id}/members/{target_user_id}")
async def remove_member(
    target_org_id: int,
    target_user_id: int,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a member from the caller's active org.

    The owner can't be removed — transfer ownership first (not in scope
    this phase; manual SQL for now).
    """
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = await _membership(session, user_id=user_id, org_id=org_id)
    _require_admin(me)

    target = await _membership(
        session, user_id=target_user_id, org_id=org_id,
    )
    if target is None:
        raise HTTPException(404, "User is not a member of this org")
    if target.role == "owner":
        raise HTTPException(
            400, "Cannot remove the owner — transfer ownership first",
        )

    await session.delete(target)
    await session.commit()
    return {"removed": True}


@router.patch("/orgs/{target_org_id}/members/{target_user_id}")
async def change_role(
    target_org_id: int,
    target_user_id: int,
    payload: RoleChangeRequest,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Owner-only — change another member's role within the active org."""
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = await _membership(session, user_id=user_id, org_id=org_id)
    _require_owner(me)

    if payload.role not in _ROLES_INVITABLE:
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    target = await _membership(
        session, user_id=target_user_id, org_id=org_id,
    )
    if target is None:
        raise HTTPException(404, "User is not a member of this org")
    if target.role == "owner":
        raise HTTPException(400, "Cannot demote the owner")

    target.role = payload.role
    await session.commit()
    return {"user_id": target_user_id, "role": payload.role}
