"""Durable private interview state and versioned biography chapters."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class InterviewSession(Base):
    __tablename__ = "life_interview_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    skipped_topics: Mapped[list] = mapped_column(JSON, default=list)
    question_topic: Mapped[str | None] = mapped_column(String(32))
    question_text: Mapped[str | None] = mapped_column(Text)
    question_source_refs: Mapped[list] = mapped_column(JSON, default=list)
    pending_turn_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InterviewTurn(Base):
    __tablename__ = "life_interview_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id"),
        UniqueConstraint("session_id", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("life_interview_sessions.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    question_id: Mapped[str] = mapped_column(String(100))
    question_text: Mapped[str] = mapped_column(Text)
    question_source_refs: Mapped[list] = mapped_column(JSON, default=list)
    question_digest: Mapped[str] = mapped_column(String(64))
    answer_digest: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("life_sources.id", ondelete="SET NULL"), index=True)
    source_revision: Mapped[int]
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    job_revision: Mapped[int | None]
    candidate_ids: Mapped[list] = mapped_column(JSON, default=list)
    processed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BiographyChapter(Base):
    __tablename__ = "life_biography_chapters"
    __table_args__ = (UniqueConstraint("owner_id", "topic"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    topic: Mapped[str] = mapped_column(String(32))
    current_version: Mapped[int] = mapped_column(default=0)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))


class BiographyVersion(Base):
    __tablename__ = "life_biography_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("life_biography_chapters.id"), index=True)
    version: Mapped[int]
    origin: Mapped[str] = mapped_column(String(16))
    markdown: Mapped[str] = mapped_column(Text)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    fact_refs: Mapped[list] = mapped_column(JSON, default=list)
    requires_cloud: Mapped[bool] = mapped_column(default=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
