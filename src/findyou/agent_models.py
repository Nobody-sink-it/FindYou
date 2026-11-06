"""Durable agent runs, source-linked drafts, missions and usage reservations."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_run_owner_time", "owner_id", "created_at"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    dedup_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    role_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentDraft(Base):
    __tablename__ = "agent_drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    target_id: Mapped[str] = mapped_column(String)
    contact_id: Mapped[str] = mapped_column(String)
    role_version_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    versions: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    simulated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentMission(Base):
    __tablename__ = "agent_missions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentUsageReservation(Base):
    __tablename__ = "agent_usage_reservations"
    __table_args__ = (UniqueConstraint("run_id", "operation"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    operation: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentFeedback(Base):
    __tablename__ = "agent_feedback"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("agent_drafts.id"))
    verdict: Mapped[str] = mapped_column(String)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    original_sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OwnerStylePreference(Base):
    __tablename__ = "owner_style_preferences"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    feedback_id: Mapped[str] = mapped_column(ForeignKey("agent_feedback.id"), unique=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="candidate")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
