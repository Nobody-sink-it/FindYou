"""Explicit evidence packages; platform text never controls persona or permissions."""

import hashlib
import json

from sqlalchemy import select

from findyou.agent_models import OwnerStylePreference
from findyou.errors import AppError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str, separators=(",", ":")).encode()).hexdigest()


class ContextBuilder:
    def __init__(self, settings, social, identity, storage):
        self.settings, self.social, self.identity, self.storage = settings, social, identity, storage

    def build(self, target_id, role_version_id=None, objective="参与有意义的讨论，了解不同观点"):
        target = self.social.get_message(target_id)
        role = self.identity.select_role(role_version_id)
        contact = self.identity.observe_message(target)
        thread = self.social.get_thread(target["thread_id"])
        messages = self.social.list_messages(target["thread_id"], limit=100)["items"]
        by_external = {m["external_id"]: m for m in messages}
        by_external[target["external_id"]] = target
        path, current, seen = [], target, set()
        missing = False
        while current and current["id"] not in seen:
            seen.add(current["id"])
            path.insert(0, current)
            if current.get("is_root"):
                break
            parent = current.get("parent_external_id")
            current = by_external.get(parent) if parent else None
            if current is None or len(path) >= 12:
                missing = True
                break
        memories = self.identity.retrieve(contact["id"], query=target["body"][:500], limit=8)
        owner = self.storage.retrieve(target["body"][:500], scope="public", limit=8, purpose="bilibili")
        with self.storage.db.sessions() as session:
            style = [{"id": p.id, "text": p.text} for p in session.scalars(select(OwnerStylePreference).where(
                OwnerStylePreference.owner_id == self.settings.owner_id,
                OwnerStylePreference.status == "confirmed").order_by(OwnerStylePreference.created_at.desc()).limit(8))]
        # Only the known path and a few already acquired related messages, never a fictional parent.
        neighbors = [m for m in messages if m["id"] not in seen][:4]
        video = thread.get("video") or self.social.get_video(target["video_id"])
        def clean(message):
            return {k: message.get(k) for k in (
                "id", "video_id", "thread_id", "external_id", "root_external_id", "parent_external_id",
                "author_uid", "author_name", "body", "posted_at", "is_root", "parent_missing",
                "source_content_id", "source_snapshot_id")}

        video_fields = ("id", "external_id", "title", "description", "coverage", "subtitle_status",
                        "metadata_source_content_id", "metadata_source_snapshot_id", "subtitle_text",
                        "subtitle_source_content_id", "subtitle_source_snapshot_id")
        package = {
            "objective": objective, "scope": "public", "target": clean(target),
            "video": {k: video.get(k) for k in video_fields},
            "path": [clean(m) for m in path], "related_messages": [clean(m) for m in neighbors],
            "contact": {k: contact.get(k) for k in ("id", "platform", "author_uid", "author_name")},
            "contact_memories": memories, "owner_evidence": owner,
            "context_incomplete": missing or bool(target.get("parent_missing")),
            "coverage": thread.get("coverage", {"status": "partial"}),
            "role_version_id": role["id"], "role_sha256": role["sha256"],
            "role_card": role["card"], "omitted_related_messages": 0,
            "approved_expression_preferences": style,
        }
        # UTF-8 byte count is a deliberately conservative token estimate; not billed usage.
        while self.estimate(package) > self.settings.agent_context_tokens and package["related_messages"]:
            package["related_messages"].pop()
            package["omitted_related_messages"] += 1
        if self.estimate(package) > self.settings.agent_context_tokens:
            raise AppError("context_budget_exceeded", "Full role and required context exceed input budget", 422)
        package["input_estimate_upper_bound"] = self.estimate(package)
        return package

    def estimate(self, package):
        return len(json.dumps(package, ensure_ascii=False, default=str).encode()) + 1200 + self.settings.llm_max_tokens

    def validate(self, package):
        """Called again immediately before persisting model output under the writer reservation."""
        for previous in package["path"] + package["related_messages"]:
            current = self.social.get_message(previous["id"])
            if digest(current["body"]) != digest(previous["body"]):
                raise AppError("evidence_changed", "Platform context changed during generation", 409)
        with self.storage.db.sessions() as session:
            for previous in package.get("approved_expression_preferences", []):
                current = session.get(OwnerStylePreference, previous["id"])
                if (not current or current.owner_id != self.settings.owner_id or current.status != "confirmed"
                        or current.text != previous["text"]):
                    raise AppError("evidence_changed", "Expression preference changed during generation", 409)
        current_video = self.social.get_video(package["video"]["id"])
        for key in ("title", "description", "subtitle_text", "metadata_source_content_id",
                    "metadata_source_snapshot_id", "subtitle_source_content_id", "subtitle_source_snapshot_id"):
            if current_video.get(key) != package["video"].get(key):
                raise AppError("evidence_changed", "Video evidence changed during generation", 409)
        for old in package["owner_evidence"]:
            if old.get("life_share_id"):
                continue  # Explicit excerpts are checked by the purpose-scoped retrieval below.
            current = self.storage.get_memory(old["id"])
            if current["status"] != "confirmed" or current["visibility"] != "public":
                raise AppError("evidence_changed", "Owner evidence is no longer eligible", 409)
        fresh = {m["id"]: m for m in self.storage.retrieve(package["target"]["body"][:500], "public",
                                                          limit=8, purpose="bilibili")}
        if any(m["id"] not in fresh or fresh[m["id"]] != m for m in package["owner_evidence"]):
            raise AppError("evidence_changed", "Owner evidence was corrected or revoked", 409)
        fresh_contact = {m["id"]: m for m in self.identity.retrieve(package["contact"]["id"],
                                      query=package["target"]["body"][:500], limit=8)}
        if any(m["id"] not in fresh_contact or fresh_contact[m["id"]] != m
               for m in package["contact_memories"]):
            raise AppError("evidence_changed", "Contact memory changed during generation", 409)
