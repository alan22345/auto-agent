"""Shared Pydantic types used across services for type-safe data exchange.

These models are the canonical types for inter-service communication.
All API responses and parsed external data should go through these.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --- Classifier types ---


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClassificationResult(BaseModel):
    classification: Literal["simple", "complex"]
    reasoning: str = ""
    estimated_files: int = 0
    risk: RiskLevel = RiskLevel.LOW


# --- Task API types (used by all services that talk to the orchestrator) ---


class TaskData(BaseModel):
    """Typed representation of a task from the orchestrator API."""
    id: int
    title: str
    description: str
    source: str
    status: str
    complexity: str | None = None
    repo_name: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    plan: str | None = None
    error: str | None = None
    created_at: str | None = None


class RepoData(BaseModel):
    """Typed representation of a repo from the orchestrator API."""
    id: int
    name: str
    url: str
    default_branch: str = "main"
    summary: str | None = None
    summary_updated_at: str | None = None
    ci_checks: str | None = None
    harness_onboarded: bool = False
    harness_pr_url: str | None = None


# --- GitHub types ---


class PRReviewComment(BaseModel):
    """A review comment from a GitHub PR."""
    author: str
    body: str
    type: Literal["review", "inline"]
    path: str = ""
    line: int | None = None


class CIStatus(BaseModel):
    """CI status for a commit."""
    sha: str
    state: Literal["success", "failure", "pending", "error"]
    message: str = ""


# --- Metrics types ---


class PROutcomeMetrics(BaseModel):
    total: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate_pct: float = 0.0
    avg_review_rounds: float = 0.0
    avg_completion_seconds: float | None = None


class MetricsResponse(BaseModel):
    period_days: int
    total_tasks: int
    active_tasks: int
    success_rate_pct: float
    by_status: dict[str, int]
    by_complexity: dict[str, int]
    by_source: dict[str, int]
    avg_duration_hours: float | None
    pr_outcomes: PROutcomeMetrics


class TimelineEntry(BaseModel):
    from_status: str | None = Field(None, alias="from")
    to_status: str = Field(alias="to")
    message: str = ""
    timestamp: str | None = None

    model_config = {"populate_by_name": True}


class TaskMetricsResponse(BaseModel):
    task_id: int
    timeline: list[TimelineEntry]
    time_in_status_seconds: dict[str, float]


# --- Feedback types ---


class FeedbackSummary(BaseModel):
    total_outcomes: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate: float = 0.0
    avg_review_rounds: float = 0.0


class OutcomeResponse(BaseModel):
    task_id: int
    pr_approved: bool
    review_rounds: int


# --- Schedule types ---


class ScheduleResponse(BaseModel):
    id: int
    name: str
    cron: str
    task_title: str
    enabled: bool
    last_run_at: str | None = None


class RepoResponse(BaseModel):
    id: int
    name: str
    url: str


# --- Linear types ---


class LinearIssue(BaseModel):
    """A Linear issue returned from the GraphQL API."""
    id: str
    identifier: str
    title: str
    description: str = ""
    state: dict[str, str] = Field(default_factory=dict)
    url: str = ""
