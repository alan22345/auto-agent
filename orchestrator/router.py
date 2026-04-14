"""FastAPI routes — internal API for task management."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_session
from shared.events import Event
from shared.models import (
    FreeformConfig,
    Repo,
    ScheduledTask,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskHistory,
    TaskSource,
    TaskStatus,
)
from shared.redis_client import get_redis, publish_event
from shared.types import (
    FeedbackSummary,
    FreeformConfigData,
    OutcomeResponse,
    RepoData,
    RepoResponse,
    ScheduleResponse,
    SuggestionData,
    TaskData,
)

from orchestrator.deduplicator import find_duplicate_by_source_id, find_duplicate_by_title
from orchestrator.feedback import analyze_patterns, get_feedback_summary, record_outcome
from orchestrator.freeform import promote_task_to_main, revert_task_from_dev
from orchestrator.state_machine import InvalidTransition, get_task, transition

router = APIRouter()

# --- Rate limiting ---

TASK_CREATION_RATE_LIMIT = 10  # max tasks per window
TASK_CREATION_WINDOW = 60  # seconds
_task_creation_timestamps: list[float] = []


def _check_rate_limit() -> None:
    """Raise 429 if task creation rate limit is exceeded."""
    now = time.monotonic()
    # Prune old timestamps
    while _task_creation_timestamps and _task_creation_timestamps[0] < now - TASK_CREATION_WINDOW:
        _task_creation_timestamps.pop(0)
    if len(_task_creation_timestamps) >= TASK_CREATION_RATE_LIMIT:
        raise HTTPException(429, f"Rate limit exceeded: max {TASK_CREATION_RATE_LIMIT} tasks per {TASK_CREATION_WINDOW}s")
    _task_creation_timestamps.append(now)


# --- Request schemas ---


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    source: TaskSource = TaskSource.MANUAL
    source_id: str = ""
    repo_name: str | None = None


class TransitionRequest(BaseModel):
    status: TaskStatus
    message: str = ""


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = ""
    # Optional override for the TaskHistory message. Used by the freeform-mode
    # auto-reviewer so its decision (and reasoning) lands in the audit log.
    message: str = ""


class RecordOutcomeRequest(BaseModel):
    pr_approved: bool
    review_rounds: int = 0
    feedback_summary: str = ""


class RegisterRepoRequest(BaseModel):
    name: str
    url: str
    default_branch: str = "main"


class CreateScheduleRequest(BaseModel):
    name: str
    cron_expression: str  # e.g. "0 9 * * 1" = every Monday 9am
    task_title: str
    task_description: str = ""
    repo_name: str | None = None


class DeleteResponse(BaseModel):
    deleted: int


class ToggleResponse(BaseModel):
    id: int
    enabled: bool


class PatternsResponse(BaseModel):
    analysis: str


# --- Task endpoints ---


@router.post("/tasks", response_model=TaskData)
async def create_task(req: CreateTaskRequest, session: AsyncSession = Depends(get_session)) -> TaskData:
    _check_rate_limit()
    # Dedup check: exact source_id → exact title
    dup = await find_duplicate_by_source_id(session, req.source_id)
    if not dup:
        dup = await find_duplicate_by_title(session, req.title)
    if dup:
        return _task_to_response(dup)

    # Resolve repo
    repo = None
    if req.repo_name:
        result = await session.execute(select(Repo).where(Repo.name == req.repo_name))
        repo = result.scalar_one_or_none()

    task = Task(
        title=req.title,
        description=req.description,
        source=req.source,
        source_id=req.source_id,
        repo_id=repo.id if repo else None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Publish event
    r = await get_redis()
    event = Event(type="task.created", task_id=task.id)
    await publish_event(r, event.to_redis())
    await r.aclose()

    return _task_to_response(task)


@router.get("/tasks", response_model=list[TaskData])
async def list_tasks(
    status: TaskStatus | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[TaskData]:
    query = select(Task).order_by(Task.created_at.desc()).limit(50)
    if status:
        query = query.where(Task.status == status)
    result = await session.execute(query)
    return [_task_to_response(t) for t in result.scalars().all()]


@router.get("/tasks/{task_id}", response_model=TaskData)
async def get_task_detail(task_id: int, session: AsyncSession = Depends(get_session)) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return _task_to_response(task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # Delete history first (FK constraint)
    await session.execute(
        select(TaskHistory).where(TaskHistory.task_id == task_id)
    )
    from sqlalchemy import delete as sql_delete
    await session.execute(sql_delete(TaskHistory).where(TaskHistory.task_id == task_id))
    await session.delete(task)
    await session.commit()

    # Publish cleanup event to free workspace
    from shared.redis_client import get_redis, publish_event
    from shared.events import Event
    r = await get_redis()
    await publish_event(r, Event(type="task.cleanup", task_id=task_id).to_redis())
    await publish_event(r, Event(type="task.failed", task_id=task_id).to_redis())
    await r.aclose()

    return {"deleted": task_id}


@router.post("/tasks/{task_id}/cancel", response_model=TaskData)
async def cancel_task(task_id: int, session: AsyncSession = Depends(get_session)) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        raise HTTPException(400, f"Task already in terminal state: {task.status.value}")
    # Force to failed regardless of current state
    task.status = TaskStatus.FAILED
    session.add(TaskHistory(
        task_id=task.id,
        from_status=task.status,
        to_status=TaskStatus.FAILED,
        message="Cancelled by user",
    ))
    await session.commit()

    from shared.redis_client import get_redis, publish_event
    from shared.events import Event
    r = await get_redis()
    await publish_event(r, Event(type="task.cleanup", task_id=task_id).to_redis())
    await publish_event(r, Event(type="task.failed", task_id=task_id).to_redis())
    await r.aclose()

    return _task_to_response(task)


@router.post("/tasks/{task_id}/transition", response_model=TaskData)
async def transition_task(
    task_id: int,
    req: TransitionRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        # Save plan text when transitioning to awaiting_approval
        if req.status == TaskStatus.AWAITING_APPROVAL and req.message.startswith("Plan:\n"):
            task.plan = req.message[len("Plan:\n"):]
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
) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    result = await session.execute(select(Repo).where(Repo.name == req.repo_name))
    repo = result.scalar_one_or_none()
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
) -> TaskData:
    task = await get_task(session, task_id)
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
) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.subtasks = req.subtasks
    task.current_subtask = req.current_subtask
    await session.commit()
    await session.refresh(task)
    return _task_to_response(task)


@router.post("/tasks/{task_id}/done", response_model=TaskData)
async def mark_task_done(task_id: int, session: AsyncSession = Depends(get_session)) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status == TaskStatus.DONE:
        return _task_to_response(task)
    if task.status != TaskStatus.AWAITING_REVIEW:
        raise HTTPException(400, f"Task is in {task.status.value}, not awaiting_review")

    r = await get_redis()
    await publish_event(r, Event(type="task.review_approved", task_id=task.id).to_redis())
    await r.aclose()

    task = await transition(session, task, TaskStatus.DONE, "Marked done by user")
    await session.commit()
    return _task_to_response(task)


@router.post("/tasks/{task_id}/approve", response_model=TaskData)
async def approve_task(
    task_id: int,
    req: ApprovalRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskData:
    task = await get_task(session, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(400, f"Task is in {task.status.value}, not awaiting_approval")

    r = await get_redis()
    if req.approved:
        approve_msg = req.message or "Plan approved by user"
        task = await transition(session, task, TaskStatus.CODING, approve_msg)
        await session.commit()
        await publish_event(r, Event(type="task.approved", task_id=task.id).to_redis())
    else:
        # Clear the old plan and re-run planning with feedback
        task.plan = None
        reject_msg = req.message or f"Plan rejected: {req.feedback}"
        task = await transition(session, task, TaskStatus.PLANNING, reject_msg)
        await session.commit()
        await publish_event(
            r,
            Event(
                type="task.rejected",
                task_id=task.id,
                payload={"feedback": req.feedback},
            ).to_redis(),
        )
        await publish_event(
            r,
            Event(
                type="task.start_planning",
                task_id=task.id,
                payload={"feedback": req.feedback},
            ).to_redis(),
        )
    await r.aclose()

    return _task_to_response(task)


# --- Feedback/Learning endpoints ---


@router.post("/tasks/{task_id}/outcome", response_model=OutcomeResponse)
async def record_task_outcome(
    task_id: int,
    req: RecordOutcomeRequest,
    session: AsyncSession = Depends(get_session),
) -> OutcomeResponse:
    task = await get_task(session, task_id)
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


@router.get("/feedback/summary", response_model=FeedbackSummary)
async def feedback_summary(session: AsyncSession = Depends(get_session)) -> FeedbackSummary:
    return await get_feedback_summary(session)


@router.get("/feedback/patterns", response_model=PatternsResponse)
async def feedback_patterns(session: AsyncSession = Depends(get_session)) -> PatternsResponse:
    analysis = await analyze_patterns(session)
    return PatternsResponse(analysis=analysis)


# --- Schedule endpoints ---


@router.post("/schedules", response_model=ScheduleResponse)
async def create_schedule(
    req: CreateScheduleRequest,
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse:
    schedule = ScheduledTask(
        name=req.name,
        cron_expression=req.cron_expression,
        task_title=req.task_title,
        task_description=req.task_description,
        repo_name=req.repo_name,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return _schedule_to_response(schedule)


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(session: AsyncSession = Depends(get_session)) -> list[ScheduleResponse]:
    result = await session.execute(select(ScheduledTask).order_by(ScheduledTask.name))
    return [_schedule_to_response(s) for s in result.scalars().all()]


@router.delete("/schedules/{schedule_id}", response_model=DeleteResponse)
async def delete_schedule(schedule_id: int, session: AsyncSession = Depends(get_session)) -> DeleteResponse:
    result = await session.execute(select(ScheduledTask).where(ScheduledTask.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    await session.delete(schedule)
    await session.commit()
    return DeleteResponse(deleted=schedule_id)


@router.post("/schedules/{schedule_id}/toggle", response_model=ToggleResponse)
async def toggle_schedule(schedule_id: int, session: AsyncSession = Depends(get_session)) -> ToggleResponse:
    result = await session.execute(select(ScheduledTask).where(ScheduledTask.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    schedule.enabled = not schedule.enabled
    await session.commit()
    return ToggleResponse(id=schedule.id, enabled=schedule.enabled)


# --- Repo endpoints ---


@router.post("/repos", response_model=RepoResponse)
async def register_repo(req: RegisterRepoRequest, session: AsyncSession = Depends(get_session)) -> RepoResponse:
    repo = Repo(name=req.name, url=req.url, default_branch=req.default_branch)
    session.add(repo)
    await session.commit()
    await session.refresh(repo)
    return RepoResponse(id=repo.id, name=repo.name, url=repo.url)


@router.get("/repos", response_model=list[RepoData])
async def list_repos(session: AsyncSession = Depends(get_session)) -> list[RepoData]:
    result = await session.execute(select(Repo).order_by(Repo.name))
    return [
        RepoData(
            id=r.id, name=r.name, url=r.url, default_branch=r.default_branch,
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
) -> dict:
    """Update a repo's default branch. Updates all entries matching the name
    (both short name like 'cardamon' and full name like 'org/cardamon').
    Body: {"default_branch": "prod"}
    """
    new_branch = req.get("default_branch", "").strip()
    if not new_branch:
        raise HTTPException(400, "default_branch is required")

    # Find all repo entries that match (short name, full name with org/)
    result = await session.execute(
        select(Repo).where(
            (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
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

    return {"repo": repo_name, "old_branch": old_branch, "new_branch": new_branch, "updated": updated}


@router.post("/repos/{repo_name}/refresh-ci")
async def refresh_repo_ci_checks(
    repo_name: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Re-extract CI checks from a repo's workflow files."""
    from orchestrator.ci_extractor import extract_ci_checks
    result = await session.execute(
        select(Repo).where(
            (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
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
) -> dict:
    """Mark a repo as harness-onboarded and store the PR URL."""
    result = await session.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
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
) -> dict:
    """Trigger harness engineering onboarding for a repo. Returns immediately.

    Pass ?force=true to re-onboard a repo that was already onboarded.
    """
    result = await session.execute(
        select(Repo).where(
            (Repo.name == repo_name) | (Repo.name.endswith(f"/{repo_name}"))
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
    r = await get_redis()
    await publish_event(r, Event(
        type="repo.onboard",
        task_id=0,
        payload={"repo_id": repo.id, "repo_name": repo.name},
    ).to_redis())
    await r.aclose()

    return {"status": "onboarding_started", "repo": repo.name}


@router.get("/tasks/{task_id}/history")
async def get_task_history(task_id: int, session: AsyncSession = Depends(get_session)) -> list[dict]:
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
) -> dict:
    result = await session.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(404, "Repo not found")
    repo.summary = req.get("summary", "")
    from datetime import datetime, timezone
    repo.summary_updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


# --- Freeform / Suggestions ---


class FreeformConfigRequest(BaseModel):
    repo_name: str
    dev_branch: str = "dev"
    analysis_cron: str = "0 9 * * 1"
    enabled: bool = True
    auto_approve_suggestions: bool = False
    auto_start_tasks: bool = False


@router.post("/freeform/config", response_model=FreeformConfigData)
async def upsert_freeform_config(
    req: FreeformConfigRequest,
    session: AsyncSession = Depends(get_session),
) -> FreeformConfigData:
    """Enable or update freeform mode for a repo."""
    repo = await _get_repo_by_name(session, req.repo_name)
    if not repo:
        raise HTTPException(404, f"Repo '{req.repo_name}' not found")

    result = await session.execute(
        select(FreeformConfig).where(FreeformConfig.repo_id == repo.id)
    )
    config = result.scalar_one_or_none()
    if config:
        config.enabled = req.enabled
        config.dev_branch = req.dev_branch
        config.analysis_cron = req.analysis_cron
        config.auto_approve_suggestions = req.auto_approve_suggestions
        config.auto_start_tasks = req.auto_start_tasks
    else:
        config = FreeformConfig(
            repo_id=repo.id,
            enabled=req.enabled,
            dev_branch=req.dev_branch,
            analysis_cron=req.analysis_cron,
            auto_approve_suggestions=req.auto_approve_suggestions,
            auto_start_tasks=req.auto_start_tasks,
        )
        session.add(config)
    await session.commit()
    await session.refresh(config)
    return _freeform_config_to_response(config, repo.name)


@router.get("/freeform/config", response_model=list[FreeformConfigData])
async def list_freeform_configs(
    session: AsyncSession = Depends(get_session),
) -> list[FreeformConfigData]:
    result = await session.execute(select(FreeformConfig))
    configs = result.scalars().all()
    out = []
    for c in configs:
        repo_result = await session.execute(select(Repo).where(Repo.id == c.repo_id))
        repo = repo_result.scalar_one_or_none()
        out.append(_freeform_config_to_response(c, repo.name if repo else None))
    return out


@router.delete("/freeform/config/{config_id}")
async def delete_freeform_config(
    config_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(FreeformConfig).where(FreeformConfig.id == config_id))
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
) -> dict:
    """Create a brand-new GitHub repo from a natural-language description.

    Picks a name via Claude, creates the repo on GitHub (private, auto_init),
    enables freeform mode for it, and queues a scaffold task that runs through
    the normal coding pipeline with auto-approval.
    """
    from orchestrator.create_repo import CreateRepoError, create_repo_and_scaffold_task

    if not req.description.strip():
        raise HTTPException(400, "description is required")

    try:
        repo, task = await create_repo_and_scaffold_task(
            session,
            description=req.description,
            org_override=req.org,
            private=req.private,
            loop=req.loop,
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
) -> dict:
    """Manually trigger a PO analysis for a repo."""
    repo = await _get_repo_by_name(session, repo_name)
    if not repo:
        raise HTTPException(404, f"Repo '{repo_name}' not found")

    r = await get_redis()
    await publish_event(
        r,
        Event(type="po.analyze", task_id=0, payload={"repo_id": repo.id, "repo_name": repo.name}).to_redis(),
    )
    await r.aclose()
    return {"ok": True, "message": f"PO analysis triggered for {repo_name}"}


@router.get("/suggestions", response_model=list[SuggestionData])
async def list_suggestions(
    status: str | None = None,
    repo_name: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[SuggestionData]:
    query = select(Suggestion).order_by(Suggestion.created_at.desc()).limit(100)
    if status:
        query = query.where(Suggestion.status == status)
    if repo_name:
        repo = await _get_repo_by_name(session, repo_name)
        if repo:
            query = query.where(Suggestion.repo_id == repo.id)
    result = await session.execute(query)
    suggestions = result.scalars().all()
    return [await _suggestion_to_response(session, s) for s in suggestions]


@router.post("/suggestions/{suggestion_id}/approve", response_model=TaskData)
async def approve_suggestion(
    suggestion_id: int,
    session: AsyncSession = Depends(get_session),
) -> TaskData:
    """Approve a suggestion — creates a freeform task."""
    result = await session.execute(select(Suggestion).where(Suggestion.id == suggestion_id))
    suggestion = result.scalar_one_or_none()
    if not suggestion:
        raise HTTPException(404, "Suggestion not found")
    if suggestion.status != SuggestionStatus.PENDING:
        raise HTTPException(400, f"Suggestion is already {suggestion.status.value}")

    # Create task from suggestion
    task = Task(
        title=suggestion.title,
        description=suggestion.description,
        source=TaskSource.FREEFORM,
        source_id=f"suggestion:{suggestion.id}",
        repo_id=suggestion.repo_id,
        freeform_mode=True,
    )
    session.add(task)
    await session.flush()

    suggestion.status = SuggestionStatus.APPROVED
    suggestion.task_id = task.id
    await session.commit()

    # Trigger task pipeline
    r = await get_redis()
    await publish_event(r, Event(type="task.created", task_id=task.id).to_redis())
    await r.aclose()

    return _task_to_response(task)


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Suggestion).where(Suggestion.id == suggestion_id))
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
) -> dict:
    """Promote a completed freeform task's changes from dev to main."""
    task = await get_task(session, task_id)
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
    pr_url = await promote_task_to_main(task.pr_url, branch_name, repo_url)
    if not pr_url:
        raise HTTPException(500, "Failed to create promotion PR")
    return {"ok": True, "pr_url": pr_url}


@router.post("/freeform/{task_id}/revert")
async def revert_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revert a freeform task's changes from the dev branch."""
    task = await get_task(session, task_id)
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

    revert_url = await revert_task_from_dev(task.pr_url, dev_branch)
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
        repo_name=repo.name if repo else None,
        title=s.title,
        description=s.description,
        rationale=s.rationale,
        category=s.category or "",
        priority=s.priority or 3,
        status=s.status.value if s.status else "pending",
        task_id=s.task_id,
        created_at=s.created_at.isoformat() if s.created_at else None,
    )


def _freeform_config_to_response(c: FreeformConfig, repo_name: str | None) -> FreeformConfigData:
    return FreeformConfigData(
        id=c.id,
        repo_name=repo_name,
        enabled=c.enabled or False,
        dev_branch=c.dev_branch or "dev",
        analysis_cron=c.analysis_cron or "0 9 * * 1",
        auto_approve_suggestions=c.auto_approve_suggestions or False,
        auto_start_tasks=c.auto_start_tasks or False,
        last_analysis_at=c.last_analysis_at.isoformat() if c.last_analysis_at else None,
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
        subtasks=task.subtasks,
        current_subtask=task.current_subtask,
        created_at=task.created_at.isoformat() if task.created_at else None,
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
