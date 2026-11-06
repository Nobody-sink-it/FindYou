"""Owner-scoped contact memories and immutable expression role cards.

Contact evidence deliberately has no foreign key to source messages: revocation
must not erase version history. Only source IDs and hashes are retained here.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from findyou.db import Base, new_id, utcnow


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("owner_id", "platform", "author_uid"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40))
    author_uid: Mapped[str] = mapped_column(String(200))
    author_name: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContactMemory(Base):
    __tablename__ = "contact_memories"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    proposal_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    versions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExpressionRole(Base):
    __tablename__ = "expression_roles"
    __table_args__ = (UniqueConstraint("owner_id", "builtin_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    builtin_key: Mapped[str | None] = mapped_column(String(40))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExpressionRoleVersion(Base):
    __tablename__ = "expression_role_versions"
    __table_args__ = (UniqueConstraint("role_id", "version"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    role_id: Mapped[str] = mapped_column(ForeignKey("expression_roles.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    card: Mapped[dict | str] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    approval_origin: Mapped[str | None] = mapped_column(String(16))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdentityPreference(Base):
    __tablename__ = "identity_preferences"
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    default_role_version_id: Mapped[str | None] = mapped_column(ForeignKey("expression_role_versions.id"))


class ExpressionFeedback(Base):
    __tablename__ = "expression_feedback"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(16))
    edited_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    original_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
