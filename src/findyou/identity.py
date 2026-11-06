"""Contact-scoped evidence retrieval; role approval and expression feedback.

These records never become owner facts or claims of friendship. No model or
platform is invoked; ``social.get_message`` supplies currently readable sources.
"""

import copy
import hashlib
import json
import re

from sqlalchemy import func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from findyou.db import new_id, utcnow
from findyou.errors import AppError
from findyou.identity_models import (
    Contact,
    ContactMemory,
    ExpressionFeedback,
    ExpressionRole,
    ExpressionRoleVersion,
    IdentityPreference,
)
from findyou.ops import Action, AuditEvent

BUILTIN_CARD = {
    "identity": "公开说明自己是协助主人的AI。角色只控制表达，不能改写主人事实或授权。",
    "purpose": "先理解具体语境，温和倾听，有必要才参与；允许跳过和结束讨论。",
    "voice": "自然、平实、具体；通常一到三句，避免空泛安慰、说教、讨好和过度亲密。",
    "listening": "回应对方实际说过的话；区分引用、反讽和本人的观点；不确定时提出一个低压力问题。",
    "disagreement": "说明依据和不确定性，尊重分歧；不攻击身份、不纠缠、不要求对方回应。",
    "evidence": "对方自述只作为自述；推断明确标注，不把记忆或角色示例当成客观事实。",
    "owner": "主人经历、立场和隐私仅使用当前允许的本人资料；没有依据时明确不知道。",
    "boundaries": "不编造经历、故事、关系或感情，不冒充真人，不替主人许诺、提供联系方式或披露隐私。",
    "stop": "对方拒绝、语境不足、重复争执或授权和预算不足时停止；归档与草稿不算实际交流。",
    "learning": "主人接受一次草稿不等于确认事实；修改是当前表达反馈，不自动证明稳定人格。",
}


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _iso(value):
    return value.isoformat(timespec="microseconds") + "Z" if value else None


def _terms(value):
    """Deterministic word/character shingles, without claiming semantic understanding."""
    terms = set(re.findall(r"[a-z0-9]+", value.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        if len(run) == 1:
            terms.add(run)
        for width in (2, 3):
            terms.update(run[start:start + width] for start in range(len(run) - width + 1))
    return terms


class IdentityService:
    def __init__(self, settings, db, social):
        self.settings, self.db, self.social = settings, db, social

    @staticmethod
    def _string(value, label, maximum=10000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise AppError("invalid_identity_input", f"{label} must be nonempty text of at most {maximum} characters.")
        return value.strip()

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise AppError("invalid_pagination", "Limit must be 1..100 and offset nonnegative.")

    def _owned(self, session, cls, identifier):
        row = session.get(cls, identifier)
        if row is None or row.owner_id != self.settings.owner_id:
            raise AppError("identity_not_found", "Identity resource was not found.", 404)
        return row

    def _audit(self, session, event, identifier, **detail):
        session.add(AuditEvent(event_type=event, resource_id=identifier,
                               detail={"owner_id": self.settings.owner_id, **detail}))

    @staticmethod
    def _contact_dict(row):
        return {"id": row.id, "owner_id": row.owner_id, "platform": row.platform,
                "author_uid": row.author_uid, "author_name": row.author_name,
                "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
                "relationship": "unknown"}

    def observe_message(self, message_dict) -> dict:
        if not isinstance(message_dict, dict):
            raise AppError("invalid_message", "Message must be an object.")
        if message_dict.get("owner_id", self.settings.owner_id) != self.settings.owner_id:
            raise AppError("identity_not_found", "Message was not found.", 404)
        uid = self._string(message_dict.get("author_uid"), "author_uid", 200)
        platform = self._string(message_dict.get("platform") or "bilibili", "platform", 40)
        name = message_dict.get("author_name") or ""
        if not isinstance(name, str) or len(name) > 300:
            raise AppError("invalid_message", "Invalid author name.")
        with self.db.sessions.begin() as session:
            session.execute(sqlite_insert(Contact).values(
                id=new_id(), owner_id=self.settings.owner_id, platform=platform,
                author_uid=uid, author_name=name.strip(),
            ).on_conflict_do_nothing(index_elements=["owner_id", "platform", "author_uid"]))
            row = session.scalar(select(Contact).filter_by(
                owner_id=self.settings.owner_id, platform=platform, author_uid=uid))
            if name.strip() and row.author_name != name.strip():
                row.author_name, row.updated_at = name.strip(), utcnow()
            return self._contact_dict(row)

    def get_contact(self, id) -> dict:
        with self.db.sessions() as session:
            return self._contact_dict(self._owned(session, Contact, id))

    def list_contacts(self, limit=20, offset=0, q=None) -> dict:
        self._page(limit, offset)
        if q is not None and (not isinstance(q, str) or len(q) > 1000):
            raise AppError("invalid_query", "Query must be text of at most 1000 characters.")
        with self.db.sessions() as session:
            query = select(Contact).where(Contact.owner_id == self.settings.owner_id)
            if q and q.strip():
                query = query.where(or_(Contact.author_name.contains(q.strip(), autoescape=True),
                                        Contact.author_uid.contains(q.strip(), autoescape=True)))
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(Contact.updated_at.desc(), Contact.id).limit(limit).offset(offset))
            return {"items": [self._contact_dict(row) for row in rows], "total": total,
                    "limit": limit, "offset": offset}

    def _read_evidence(self, contact, identifier):
        message = self.social.get_message(identifier)
        if (not isinstance(message, dict) or message.get("id") != identifier
                or message.get("owner_id", self.settings.owner_id) != self.settings.owner_id
                or message.get("author_uid") != contact.author_uid
                or (message.get("platform") or "bilibili") != contact.platform
                or not isinstance(message.get("body"), str)
                or message.get("visibility") == "private"):
            raise AppError("invalid_contact_evidence", "Evidence must be readable public speech by this contact.")
        evidence = {"message_id": identifier, "body_sha256": _hash(message["body"])}
        if message.get("posted_at"):
            evidence["posted_at"] = message["posted_at"]
        return evidence

    def _evidence_current(self, contact, evidence):
        if not evidence:
            return False
        for item in evidence:
            try:
                if self._read_evidence(contact, item["message_id"]) != item:
                    return False
            except AppError:
                return False
        return True

    def _memory_dict(self, row, contact):
        versions = copy.deepcopy(row.versions)
        for version in versions:
            version["stale"] = not self._evidence_current(contact, version["evidence"])
            # Memory text may paraphrase revoked source text; redact both.
            if version["stale"] or row.status == "deleted":
                version["text"] = None
            version["epistemic_label"] = "对方自述" if version["epistemic"] == "self_report" else "推断"
        current = versions[-1]
        return {"id": row.id, "owner_id": row.owner_id, "contact_id": row.contact_id,
                "status": row.status, "current_version": row.current_version,
                "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
                "versions": versions, **{key: current[key] for key in (
                    "text", "category", "epistemic", "epistemic_label", "evidence", "stale")},
                "confirmation_scope": "owner approval of contact memory; not objective verification or owner fact"}

    def list_contact_memories(self, contact_id, limit=20, offset=0, status=None) -> dict:
        self._page(limit, offset)
        if status is not None and (not isinstance(status, str) or status not in {
                "active", "candidate", "confirmed", "inactive", "deleted"}):
            raise AppError("invalid_status", "Unknown contact memory status.")
        with self.db.sessions() as session:
            contact = self._owned(session, Contact, contact_id)
            query = select(ContactMemory).filter_by(owner_id=self.settings.owner_id, contact_id=contact_id)
            if status:
                query = query.where(ContactMemory.status == status)
            else:
                query = query.where(ContactMemory.status != "deleted")
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = list(session.scalars(query.order_by(ContactMemory.created_at.desc(), ContactMemory.id)
                                        .limit(limit).offset(offset)))
        return {"items": [self._memory_dict(row, contact) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def propose_memories(self, contact_id, facts) -> list[dict]:
        if not isinstance(facts, list) or len(facts) > 100:
            raise AppError("invalid_facts", "Facts must be a list of at most 100 entries.")
        with self.db.sessions() as session:
            contact = self._owned(session, Contact, contact_id)
        proposals = []
        for fact in facts:
            if not isinstance(fact, dict):
                raise AppError("invalid_fact", "Fact must be an object.")
            body = self._string(fact.get("text"), "text")
            category = self._string(fact.get("category", "fact"), "category", 40)
            epistemic, ids = fact.get("epistemic"), fact.get("evidence_ids")
            if not isinstance(epistemic, str) or epistemic not in {"self_report", "inference"}:
                raise AppError("invalid_epistemic", "Epistemic must be self_report or inference.")
            if (not isinstance(ids, list) or not ids or len(ids) > 100
                    or any(not isinstance(i, str) or not i.strip() for i in ids)):
                raise AppError("invalid_contact_evidence", "Evidence must contain message IDs.")
            evidence = [self._read_evidence(contact, i) for i in sorted(set(ids))]
            version = {"version": 1, "text": body, "category": category, "epistemic": epistemic,
                       "evidence": evidence, "created_at": _iso(utcnow()), "confirmed_at": None}
            key = _hash(_json([self.settings.owner_id, contact_id, body, category, epistemic, evidence]))
            proposals.append((key, version))
        with self.db.sessions.begin() as session:
            rows = []
            for key, version in proposals:
                session.execute(sqlite_insert(ContactMemory).values(
                    id=new_id(), owner_id=self.settings.owner_id, contact_id=contact_id, proposal_key=key,
                    status="active" if version["epistemic"] == "self_report" else "candidate",
                    versions=[version],
                ).on_conflict_do_nothing(index_elements=["proposal_key"]))
                rows.append(session.scalar(select(ContactMemory).where(ContactMemory.proposal_key == key)))
        return [self._memory_dict(row, contact) for row in rows]

    def retrieve(self, contact_id, query="", limit=8) -> list[dict]:
        """Rank currently eligible records by lexical overlap, then recent fallback.

        Retrieval metadata describes selection only, never the truth of a memory
        or semantic similarity. A no-overlap fallback returns at most two records.
        """
        self._page(limit, 0)
        if not isinstance(query, str) or len(query) > 10000:
            raise AppError("invalid_query", "Query must be text of at most 10000 characters.")
        with self.db.sessions() as session:
            contact = self._owned(session, Contact, contact_id)
            rows = list(session.scalars(select(ContactMemory).filter_by(
                owner_id=self.settings.owner_id, contact_id=contact_id).where(
                ContactMemory.status.in_(["active", "confirmed"])).order_by(
                ContactMemory.updated_at.desc(), ContactMemory.id)))
        eligible = []
        for row in rows:
            current = row.versions[-1]
            if (row.status != "confirmed" and current["epistemic"] != "self_report"):
                continue
            item = self._memory_dict(row, contact)
            if not item["stale"]:
                # History belongs in the owner-facing list, never in generation
                # context where superseded text could undo a correction.
                eligible.append({key: value for key, value in item.items() if key != "versions"})
        normalized = query.strip().casefold()
        terms = _terms(normalized)
        scored = []
        for item in eligible:
            memory_terms = _terms(item["text"])
            # A single Chinese character cannot form a bigram; support short
            # topic queries without reducing full sentences to common characters.
            if re.fullmatch(r"[\u3400-\u9fff]", normalized):
                score = float(normalized in item["text"])
            else:
                overlap = len(terms & memory_terms)
                score = 2 * overlap / (len(terms) + len(memory_terms)) if overlap else 0.0
            if score:
                scored.append((score, item))
        if not normalized:
            mode, selected = "recent", [(0.0, item) for item in eligible[:limit]]
        elif scored:
            mode, selected = "lexical_overlap", sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]
        else:
            mode, selected = "recent_fallback", [(0.0, item) for item in eligible[:min(limit, 2)]]
        result = []
        for score, item in selected:
            result.append({**item, "retrieval": {"mode": mode, "lexical_similarity": round(score, 6),
                                               "semantic_similarity_evaluated": False}})
        return result

    def correct_memory(self, id, text) -> dict:
        body = self._string(text, "text")
        with self.db.sessions.begin() as session:
            session.execute(sql_text("BEGIN IMMEDIATE"))
            row = self._owned(session, ContactMemory, id)
            if row.status == "deleted":
                raise AppError("identity_not_found", "Memory was deleted.", 404)
            contact = self._owned(session, Contact, row.contact_id)
            version = {**copy.deepcopy(row.versions[-1]), "version": row.current_version + 1,
                       "text": body, "created_at": _iso(utcnow()), "confirmed_at": None}
            row.versions = [*row.versions, version]
            row.current_version, row.status, row.updated_at = version["version"], "candidate", utcnow()
            self._audit(session, "contact_memory_corrected", id, version=row.current_version)
        return self._memory_dict(row, contact)

    def confirm_memory(self, id) -> dict:
        with self.db.sessions() as session:
            row = self._owned(session, ContactMemory, id)
            contact = self._owned(session, Contact, row.contact_id)
            expected_version = row.current_version
            if row.status == "deleted":
                raise AppError("identity_not_found", "Memory was deleted.", 404)
        if not self._evidence_current(contact, row.versions[-1]["evidence"]):
            raise AppError("stale_contact_evidence", "Contact evidence changed or is unavailable.", 409)
        with self.db.sessions.begin() as session:
            session.execute(sql_text("BEGIN IMMEDIATE"))
            row = self._owned(session, ContactMemory, id)
            if row.current_version != expected_version or row.status == "deleted":
                raise AppError("memory_changed", "Memory changed before confirmation.", 409)
            versions = copy.deepcopy(row.versions)
            versions[-1]["confirmed_at"] = versions[-1]["confirmed_at"] or _iso(utcnow())
            row.versions, row.status, row.updated_at = versions, "confirmed", utcnow()
            self._audit(session, "contact_memory_confirmed", id, version=expected_version)
        return self._memory_dict(row, contact)

    def _set_memory_status(self, id, status):
        with self.db.sessions.begin() as session:
            row = self._owned(session, ContactMemory, id)
            contact = self._owned(session, Contact, row.contact_id)
            if row.status == "deleted" and status != "deleted":
                raise AppError("identity_not_found", "Memory was deleted.", 404)
            row.status, row.updated_at = status, utcnow()
            self._audit(session, "contact_memory_" + status, id)
        return self._memory_dict(row, contact)

    def deactivate_memory(self, id) -> dict:
        return self._set_memory_status(id, "inactive")

    def delete_memory(self, id) -> dict:
        # A tombstone preserves original proposal identity and all version evidence.
        return self._set_memory_status(id, "deleted")

    @staticmethod
    def _card(card):
        if not isinstance(card, (dict, str)) or not card or (isinstance(card, str) and not card.strip()):
            raise AppError("invalid_role_card", "Card must be a nonempty JSON object or text.")
        try:
            encoded = _json(card)
        except (TypeError, ValueError) as exc:
            raise AppError("invalid_role_card", "Card must be valid JSON.") from exc
        if len(encoded) > 50000:
            raise AppError("invalid_role_card", "Card exceeds 50000 characters.")
        return json.loads(encoded), _hash(encoded)

    def _version(self, session, id, approved=False):
        row = session.get(ExpressionRoleVersion, id)
        if row is None:
            raise AppError("identity_not_found", "Role version was not found.", 404)
        self._owned(session, ExpressionRole, row.role_id)
        if approved and row.status != "approved":
            raise AppError("role_not_approved", "Role version requires owner approval.", 409)
        if self._card(row.card)[1] != row.sha256:
            raise AppError("role_integrity_error", "Role card hash does not match its approved version.", 409)
        return row

    @staticmethod
    def _version_dict(row):
        return {"id": row.id, "role_id": row.role_id, "version": row.version,
                "card": copy.deepcopy(row.card), "sha256": row.sha256, "status": row.status,
                "approval_origin": row.approval_origin, "approved_at": _iso(row.approved_at),
                "created_at": _iso(row.created_at)}

    def _role_dict(self, session, row):
        default = session.get(IdentityPreference, self.settings.owner_id)
        default_id = default.default_role_version_id if default else None
        current = session.scalar(select(ExpressionRoleVersion).filter_by(role_id=row.id, version=row.current_version))
        return {"id": row.id, "name": row.name, "current_version": row.current_version,
                "current_version_id": current.id, "default_version_id": default_id,
                "is_default": bool(default_id and session.get(ExpressionRoleVersion, default_id).role_id == row.id)}

    def list_roles(self) -> dict:
        with self.db.sessions() as session:
            rows = session.scalars(select(ExpressionRole).filter_by(owner_id=self.settings.owner_id)
                                   .order_by(ExpressionRole.created_at, ExpressionRole.id))
            return {"items": [self._role_dict(session, row) for row in rows]}

    def create_role(self, name, card) -> dict:
        name = self._string(name, "name", 200)
        card, digest = self._card(card)
        with self.db.sessions.begin() as session:
            row = ExpressionRole(id=new_id(), owner_id=self.settings.owner_id, name=name)
            session.add(row)
            session.flush()
            session.add(ExpressionRoleVersion(role_id=row.id, version=1, card=card, sha256=digest))
            session.flush()
            return self._role_dict(session, row)

    def add_role_version(self, role_id, card) -> dict:
        card, digest = self._card(card)
        with self.db.sessions.begin() as session:
            session.execute(sql_text("BEGIN IMMEDIATE"))
            role = self._owned(session, ExpressionRole, role_id)
            role.current_version += 1
            row = ExpressionRoleVersion(role_id=role.id, version=role.current_version, card=card, sha256=digest)
            session.add(row)
            session.flush()
            return self._version_dict(row)

    def approve_role_version(self, version_id) -> dict:
        with self.db.sessions.begin() as session:
            row = self._version(session, version_id)
            row.status = "approved"
            row.approval_origin = row.approval_origin or "owner"
            row.approved_at = row.approved_at or utcnow()
            self._audit(session, "role_version_approved", row.id, sha256=row.sha256)
            return self._version_dict(row)

    def role_versions(self, role_id) -> dict:
        with self.db.sessions() as session:
            self._owned(session, ExpressionRole, role_id)
            rows = session.scalars(select(ExpressionRoleVersion).filter_by(role_id=role_id)
                                   .order_by(ExpressionRoleVersion.version))
            return {"items": [self._version_dict(row) for row in rows]}

    def get_role_version(self, version_id) -> dict:
        with self.db.sessions() as session:
            return self._version_dict(self._version(session, version_id, approved=True))

    def select_role(self, version_id=None) -> dict:
        if version_id is not None:
            return self.get_role_version(version_id)
        with self.db.sessions() as session:
            pref = session.get(IdentityPreference, self.settings.owner_id)
            if pref and pref.default_role_version_id:
                return self._version_dict(self._version(session, pref.default_role_version_id, approved=True))
        # Builtin creation is serialized; no model or source read inside this lock.
        with self.db.sessions.begin() as session:
            session.execute(sql_text("BEGIN IMMEDIATE"))
            pref = session.get(IdentityPreference, self.settings.owner_id)
            if pref and pref.default_role_version_id:
                return self._version_dict(self._version(session, pref.default_role_version_id, approved=True))
            role = session.scalar(select(ExpressionRole).filter_by(
                owner_id=self.settings.owner_id, builtin_key="gentle_listener"))
            if role is None:
                role = ExpressionRole(id=new_id(), owner_id=self.settings.owner_id, name="温和倾听",
                                      builtin_key="gentle_listener")
                session.add(role)
                session.flush()
                card, digest = self._card(BUILTIN_CARD)
                session.add(ExpressionRoleVersion(role_id=role.id, version=1, card=card, sha256=digest,
                    status="approved", approval_origin="builtin", approved_at=utcnow()))
                session.flush()
            row = session.scalar(select(ExpressionRoleVersion).filter_by(role_id=role.id, version=1))
            row = self._version(session, row.id, approved=True)
            if pref is None:
                session.add(IdentityPreference(owner_id=self.settings.owner_id, default_role_version_id=row.id))
            else:
                pref.default_role_version_id = row.id
            return self._version_dict(row)

    def set_default_role(self, version_id) -> dict:
        with self.db.sessions.begin() as session:
            session.execute(sql_text("BEGIN IMMEDIATE"))
            row = self._version(session, version_id, approved=True)
            pref = session.get(IdentityPreference, self.settings.owner_id)
            if pref is None:
                session.add(IdentityPreference(owner_id=self.settings.owner_id, default_role_version_id=row.id))
            else:
                pref.default_role_version_id = row.id
            self._audit(session, "default_role_selected", row.id, sha256=row.sha256)
            return self._version_dict(row)

    def record_feedback(self, action_id, verdict, edited_text=None, reason="") -> dict:
        if not isinstance(verdict, str) or verdict not in {"accepted", "edited", "rejected"}:
            raise AppError("invalid_feedback", "Unknown verdict.")
        if verdict == "edited":
            edited_text = self._string(edited_text, "edited_text")
        elif edited_text is not None:
            raise AppError("invalid_feedback", "Only edited feedback may include edited_text.")
        if not isinstance(reason, str) or len(reason) > 10000:
            raise AppError("invalid_feedback", "Reason must be text of at most 10000 characters.")
        with self.db.sessions.begin() as session:
            action = self._owned(session, Action, action_id)
            row = ExpressionFeedback(action_id=action.id, owner_id=self.settings.owner_id, verdict=verdict,
                                     edited_text=edited_text, reason=reason, original_sha256=_hash(action.draft))
            session.add(row)
            session.flush()
            self._audit(session, "expression_feedback_recorded", row.id, action_id=action_id, verdict=verdict)
            return {"id": row.id, "owner_id": row.owner_id, "action_id": row.action_id,
                    "verdict": row.verdict, "edited_text": row.edited_text, "reason": row.reason,
                    "original_sha256": row.original_sha256, "created_at": _iso(row.created_at),
                    "learning_status": "recorded_only"}
