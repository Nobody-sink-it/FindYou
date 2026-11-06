"""Lifecycle metadata for owner sources, memory versions and explicit sharing."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class LifeSource(Base):
    __tablename__ = "life_sources"
    id: Mapped[str] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    cloud_allowed: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class LifeMemoryDetails(Base):
    __tablename__ = "life_memory_details"
    version_id: Mapped[str] = mapped_column(ForeignKey("memory_versions.id", ondelete="CASCADE"),
                                           primary_key=True)
    topic: Mapped[str] = mapped_column(String(20), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    source_dependencies: Mapped[list] = mapped_column(JSON, default=list)
    fact_dependencies: Mapped[list] = mapped_column(JSON, default=list)


class LifeShare(Base):
    __tablename__ = "life_shares"
    __table_args__ = (UniqueConstraint("version_id", "channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("memory_versions.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
