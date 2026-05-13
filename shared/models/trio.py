"""ORM models for the architect/builder/reviewer trio."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB

from .core import Base


class ArchitectPhase(str, enum.Enum):
    INITIAL    = "initial"
    CONSULT    = "consult"
    CHECKPOINT = "checkpoint"
    REVISION   = "revision"


class ArchitectAttempt(Base):
    __tablename__ = "architect_attempts"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    phase = Column(SAEnum(ArchitectPhase, name="architect_phase"), nullable=False)
    cycle = Column(Integer, nullable=False)
    reasoning = Column(Text, nullable=False)
    decision = Column(JSONB, nullable=True)
    consult_question = Column(Text, nullable=True)
    consult_why = Column(Text, nullable=True)
    architecture_md_after = Column(Text, nullable=True)
    commit_sha = Column(String(40), nullable=True)
    tool_calls = Column(JSONB, nullable=False, default=list, server_default="[]")
    # Set when phase=INITIAL/CHECKPOINT and decision.action=
    # "awaiting_clarification". Holds the prose question architect asked
    # and the prose answer the PO (freeform) or user (non-freeform)
    # gave. session_blob_path is the relative path under the workspace
    # tree where Session.save() persisted the AgentLoop's messages.
    clarification_question = Column(Text, nullable=True)
    clarification_answer = Column(Text, nullable=True)
    clarification_source = Column(String(16), nullable=True)  # 'user' | 'po'
    session_blob_path = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class TrioReviewAttempt(Base):
    __tablename__ = "trio_review_attempts"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle = Column(Integer, nullable=False)
    ok = Column(Boolean, nullable=False)
    feedback = Column(Text, nullable=False, default="", server_default="")
    tool_calls = Column(JSONB, nullable=False, default=list, server_default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
