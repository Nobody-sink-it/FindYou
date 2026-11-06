"""Content, source provenance and explicitly confirmed owner memory."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Content(Base):
    __tablename__ = "contents"
    __table_args__ = (
        UniqueConstraint("owner_id", "platform", "authorship", "sha256", "format", name="uq_content_identity"),
        CheckConstraint("visibility IN ('private','public')"),
        CheckConstraint("authorship IN ('owner','external')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    platform: Mapped[str] = mapped_column(String(40))
    authorship: Mapped[str] = mapped_column(String(16))
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    format: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    raw_path: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("source_snapshots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ContentBlock(Base):
    __tablename__ = "content_blocks"
    __table_args__ = (UniqueConstraint("content_id", "position"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id", ondelete="CASCADE"))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="text")
    locator: Mapped[dict] = mapped_column(JSON, default=dict)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("status IN ('candidate','confirmed','inactive')"),
        CheckConstraint("visibility IN ('private','public')"),
        Index("ix_memory_owner_status", "owner_id", "status"),
        Index("uq_memory_proposal_key", "proposal_key", unique=True),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    # Immutable identity of the original extraction; user edits never replace it.
    proposal_key: Mapped[str | None] = mapped_column(String(64))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"
    __table_args__ = (UniqueConstraint("memory_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(40), default="fact")
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryEvidence(Base):
    __tablename__ = "memory_evidence"
    version_id: Mapped[str] = mapped_column(ForeignKey("memory_versions.id", ondelete="CASCADE"), primary_key=True)
    block_id: Mapped[str] = mapped_column(ForeignKey("content_blocks.id", ondelete="CASCADE"), primary_key=True)
