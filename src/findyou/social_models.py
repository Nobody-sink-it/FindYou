"""Stable platform identities and append-only, source-gated social observations."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class Video(Base):
    __tablename__ = "social_videos"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), default="bilibili")
    external_id: Mapped[str] = mapped_column(String(200))


class Thread(Base):
    __tablename__ = "social_threads"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "root_external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), default="bilibili")
    video_id: Mapped[str] = mapped_column(ForeignKey("social_videos.id"), index=True)
    root_external_id: Mapped[str] = mapped_column(String(200))


class PlatformIdentity(Base):
    __tablename__ = "platform_identities"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), default="bilibili")
    external_id: Mapped[str] = mapped_column(String(200))


class Message(Base):
    __tablename__ = "social_messages"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), default="bilibili")
    video_id: Mapped[str] = mapped_column(ForeignKey("social_videos.id"), index=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("social_threads.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    root_external_id: Mapped[str] = mapped_column(String(200))
    parent_external_id: Mapped[str | None] = mapped_column(String(200))


class SocialObservation(Base):
    __tablename__ = "social_observations"
    __table_args__ = (
        UniqueConstraint("owner_id", "dedup_key"),
        Index("ix_social_observation_entity_time", "video_id", "message_id", "observed_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("social_videos.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("social_messages.id"), index=True)
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("platform_identities.id"), index=True)
    operation: Mapped[str] = mapped_column(String(24))
    # Retain observations after source deletion, but never serve orphan observations.
    source_content_id: Mapped[str | None] = mapped_column(
        ForeignKey("contents.id", ondelete="SET NULL"), index=True)
    source_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="SET NULL"), index=True)
    # Immutable audit references survive FK nulling when the original source is deleted.
    source_content_ref: Mapped[str] = mapped_column(String(36))
    source_snapshot_ref: Mapped[str] = mapped_column(String(36))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dedup_key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
