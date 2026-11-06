"""Operational state; platform writes are deliberately unavailable in this release."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("owner_id", "account_key", "platform", "peer_id", "thread_key", "scope"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("owners.id"))
    account_key: Mapped[str] = mapped_column(String, default="local")
    platform: Mapped[str] = mapped_column(String, default="simulation")
    peer_id: Mapped[str] = mapped_column(String)
    thread_key: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String, default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "external_uid"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("owners.id"))
    platform: Mapped[str] = mapped_column(String)
    external_uid: Mapped[str] = mapped_column(String)
    auth_status: Mapped[str] = mapped_column(String)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "external_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String, ForeignKey("conversations.id", ondelete="CASCADE"))
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    conversation_id: Mapped[str] = mapped_column(String, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("owners.id"))
    conversation_id: Mapped[str] = mapped_column(String, ForeignKey("conversations.id"))
    incoming_message_id: Mapped[str] = mapped_column(String, ForeignKey("messages.id"), unique=True)
    kind: Mapped[str] = mapped_column(String, default="reply_draft")
    status: Mapped[str] = mapped_column(String, default="draft")
    draft: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    simulated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    simulated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
