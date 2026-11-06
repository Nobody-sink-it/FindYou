"""Transactional content imports and provenance-gated owner memory retrieval."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from findyou.config import Settings
from findyou.content_models import (
    Content,
    ContentBlock,
    Memory,
    MemoryEvidence,
    MemoryVersion,
    SourceSnapshot,
)
from findyou.contracts import ImportRequest
from findyou.db import Database, new_id, utcnow
from findyou.errors import AppError
from findyou.life_models import LifeMemoryDetails, LifeSource


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds") + "Z" if value else None


class Storage:
    def __init__(self, settings: Settings, database: Database | None = None):
        self.settings = settings
        self.db = database or Database(settings)
        self.life = None

    def _audit(self, session, event_type, resource_id, **detail):
        from findyou.ops import AuditEvent

        session.add(AuditEvent(event_type=event_type, resource_id=resource_id,
                               detail={"owner_id": self.settings.owner_id, **detail}))

    @staticmethod
    def _page(limit, offset):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AppError("invalid_limit", "Limit must be an integer from 1 to 100.")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise AppError("invalid_offset", "Offset must be a nonnegative integer.")

    @staticmethod
    def _visibility(value):
        if value not in {"private", "public"}:
            raise AppError("invalid_visibility", "Visibility must be private or public.")

    def _content(self, session, identifier):
        content = session.get(Content, identifier)
        if content is None or content.owner_id != self.settings.owner_id:
            raise AppError("content_not_found", "Content was not found.", 404)
        return content

    def _memory(self, session, identifier):
        memory = session.get(Memory, identifier)
        if memory is None or memory.owner_id != self.settings.owner_id:
            raise AppError("memory_not_found", "Memory was not found.", 404)
        return memory

    @staticmethod
    def _version(session, memory):
        return session.scalar(select(MemoryVersion).where(
            MemoryVersion.memory_id == memory.id, MemoryVersion.version == memory.current_version
        ))

    @staticmethod
    def _blocks(session, content_id):
        return [{"id": block.id, "text": block.text, "position": block.position,
                 "kind": block.kind, "snapshot_id": block.snapshot_id, "locator": block.locator}
                for block in session.scalars(select(ContentBlock).where(
                    ContentBlock.content_id == content_id).order_by(ContentBlock.position))]

    def _content_dict(self, session, content, detail=True):
        result = {"id": content.id, "title": content.title, "owner_id": content.owner_id,
                  "platform": content.platform, "authorship": content.authorship,
                  "visibility": content.visibility, "format": content.format,
                  "external_id": content.external_id, "source_url": content.source_url,
                  "sha256": content.sha256, "metadata": content.metadata_json,
                  "created_at": _iso(content.created_at)}
        if detail:
            result["blocks"] = self._blocks(session, content.id)
            result["snapshots"] = [{"id": snapshot.id, "sha256": snapshot.sha256,
                                    "format": snapshot.format, "raw_path": snapshot.raw_path,
                                    "source_url": snapshot.source_url, "captured_at": _iso(snapshot.captured_at)}
                                   for snapshot in session.scalars(select(SourceSnapshot).where(
                                       SourceSnapshot.content_id == content.id))]
        return result

    @staticmethod
    def _evidence(session, version_id):
        rows = session.execute(select(ContentBlock, Content).join(
            MemoryEvidence, MemoryEvidence.block_id == ContentBlock.id).join(
            Content, Content.id == ContentBlock.content_id).where(
            MemoryEvidence.version_id == version_id).order_by(ContentBlock.content_id, ContentBlock.position))
        return [{"block_id": block.id, "content_id": content.id, "title": content.title,
                 "source_url": content.source_url, "text": block.text}
                for block, content in rows]

    def _memory_dict(self, session, memory):
        versions = list(session.scalars(select(MemoryVersion).where(
            MemoryVersion.memory_id == memory.id).order_by(MemoryVersion.version)))
        current = next(v for v in versions if v.version == memory.current_version)
        result = {"id": memory.id, "owner_id": memory.owner_id, "text": current.text,
                "kind": current.kind, "status": memory.status, "visibility": memory.visibility,
                "current_version": memory.current_version, "created_at": _iso(memory.created_at),
                "updated_at": _iso(memory.updated_at), "evidence": self._evidence(session, current.id),
                "versions": [{"version": v.version, "text": v.text, "kind": v.kind,
                              "visibility": v.visibility, "confirmed_at": _iso(v.confirmed_at),
                              "created_at": _iso(v.created_at), "evidence": self._evidence(session, v.id)}
                             for v in versions]}
        if self.life:
            self.life.redact_memory(session, versions, result)
        return result

    def import_content(self, payload: ImportRequest, *, session=None, life=False) -> dict:
        if payload.platform.startswith("life:") and not life:
            raise AppError("reserved_platform", "Use the life source entry point.", 422)
        if not payload.text.strip():
            raise AppError("empty_content", "Content must contain non-whitespace text.")
        if len(payload.text) > self.settings.max_import_chars:
            raise AppError("content_too_large", "Content exceeds the configured import size.", 413)
        try:
            json.dumps(payload.metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AppError("invalid_metadata", "Metadata must be valid JSON.") from exc
        raw = payload.text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        identity = (Content.owner_id == self.settings.owner_id, Content.platform == payload.platform,
                    Content.authorship == payload.authorship, Content.sha256 == digest,
                    Content.format == payload.format)
        with (nullcontext(session) if session is not None else self.db.sessions()) as reader:
            existing = reader.scalar(select(Content).where(*identity))
            if existing:
                return {**self._content_dict(reader, existing), "duplicate": True}
        raw_dir = self.settings.data_dir.resolve() / "raw" / digest[:2]
        raw_path = raw_dir / (digest + ".txt")
        try:
            with (nullcontext(session) if session is not None else self.db.sessions.begin()) as writer:
                content = Content(id=new_id(), owner_id=self.settings.owner_id, title=payload.title,
                                  platform=payload.platform, authorship=payload.authorship,
                                  visibility=payload.visibility, format=payload.format,
                                  external_id=payload.external_id, source_url=payload.source_url,
                                  sha256=digest, metadata_json=payload.metadata)
                writer.add(content)
                writer.flush()
                # INSERT holds SQLite's writer lock. Raw cleanup takes that same lock,
                # so a concurrent deletion cannot remove an import's newly shared file.
                self._write_snapshot(raw_path, raw, digest)
                snapshot = SourceSnapshot(id=new_id(), content_id=content.id, sha256=digest,
                                          raw_path=str(raw_path), format=payload.format,
                                          source_url=payload.source_url)
                writer.add(snapshot)
                writer.flush()
                # Preserve source character offsets; paragraph boundaries improve excerpt size.
                position = 0
                for paragraph in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", payload.text):
                    for offset in range(0, len(paragraph.group()), 1800):
                        start = paragraph.start() + offset
                        block_text = paragraph.group()[offset:offset + 1800]
                        if not block_text.strip():
                            continue
                        writer.add(ContentBlock(id=new_id(), content_id=content.id, snapshot_id=snapshot.id,
                                                 position=position, text=block_text,
                                                 locator={"start": start, "end": start + len(block_text)}))
                        position += 1
                writer.flush()
                self._audit(writer, "content_imported", content.id, platform=content.platform,
                            authorship=content.authorship, visibility=content.visibility)
                result = {**self._content_dict(writer, content), "duplicate": False}
            return result
        except IntegrityError:
            if session is not None:
                raise
            # Concurrent identical imports share the raw snapshot and one immutable identity.
            with self.db.sessions() as session:
                existing = session.scalar(select(Content).where(*identity))
                if existing:
                    return {**self._content_dict(session, existing), "duplicate": True}
            raise AppError("import_conflict", "Content could not be imported due to a storage conflict.", 409)

    @staticmethod
    def _write_snapshot(path, raw, digest):
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise AppError("raw_integrity_error", "Stored source snapshot failed its integrity check.", 500)
                return
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="snapshot-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise AppError("raw_storage_error", "The source snapshot could not be saved.", 503,
                           retryable=True) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list_content(self, limit=20, offset=0) -> dict:
        self._page(limit, offset)
        with self.db.sessions() as session:
            query = select(Content).where(Content.owner_id == self.settings.owner_id)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(Content.created_at.desc(), Content.id).limit(limit).offset(offset))
            return {"items": [self._content_dict(session, row, False) for row in rows],
                    "total": total, "limit": limit, "offset": offset}

    def get_content(self, id) -> dict:
        with self.db.sessions() as session:
            return self._content_dict(session, self._content(session, id))

    def propose_facts(self, content_id, facts: list[dict], *, session=None, life=False) -> list[dict]:
        if not isinstance(facts, list) or len(facts) > 100:
            raise AppError("invalid_facts", "Facts must be a list containing at most 100 entries.")
        with (nullcontext(session) if session is not None else self.db.sessions.begin()) as session:
            content = self._content(session, content_id)
            if session.get(LifeSource, content.id) and not life:
                raise AppError("life_source", "Use the authorized life extraction workflow.", 409)
            if content.authorship != "owner":
                raise AppError("external_owner_memory_forbidden", "External content cannot become owner memory.")
            block_ids = set(session.scalars(select(ContentBlock.id).where(ContentBlock.content_id == content.id)))
            memories = []
            for fact in facts:
                if not isinstance(fact, dict):
                    raise AppError("invalid_fact", "Each fact must be an object.")
                fact_text = fact.get("text")
                evidence = fact.get("evidence_block_ids")
                kind = fact.get("kind", "fact")
                if not isinstance(fact_text, str) or not fact_text.strip() or len(fact_text) > 10000:
                    raise AppError("invalid_fact", "Fact text must contain 1 to 10000 characters.")
                if not isinstance(kind, str) or not kind.strip() or len(kind) > 40:
                    raise AppError("invalid_fact", "Fact kind must contain 1 to 40 characters.")
                if (not isinstance(evidence, list) or not evidence
                        or any(not isinstance(e, str) or e not in block_ids for e in evidence)):
                    raise AppError("invalid_provenance", "Evidence must reference blocks from this owner content.")
                kind = kind.strip()
                identity = json.dumps([self.settings.owner_id, sorted(set(evidence)), fact_text.strip(), kind],
                                      ensure_ascii=False, separators=(",", ":"))
                key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                identifier = session.scalar(sqlite_insert(Memory).values(
                    id=new_id(), owner_id=self.settings.owner_id, visibility=content.visibility,
                    proposal_key=key,
                ).on_conflict_do_nothing(index_elements=["proposal_key"]).returning(Memory.id))
                if identifier is None:
                    memories.append(session.scalar(select(Memory).where(Memory.proposal_key == key)))
                    continue
                memory = session.get(Memory, identifier)
                version = MemoryVersion(id=new_id(), memory_id=memory.id, version=1,
                                        text=fact_text.strip(), kind=kind, visibility=content.visibility)
                session.add(version)
                session.flush()
                session.add_all(MemoryEvidence(version_id=version.id, block_id=b) for b in set(evidence))
                self._audit(session, "memory_proposed", memory.id, content_id=content.id, version=1)
                memories.append(memory)
            session.flush()
            return [self._memory_dict(session, memory) for memory in memories]

    def list_memories(self, limit=20, offset=0, status=None) -> dict:
        self._page(limit, offset)
        if status not in {None, "candidate", "confirmed", "inactive"}:
            raise AppError("invalid_status", "Unknown memory status.")
        with self.db.sessions() as session:
            query = select(Memory).where(Memory.owner_id == self.settings.owner_id)
            if status:
                query = query.where(Memory.status == status)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(Memory.created_at.desc(), Memory.id).limit(limit).offset(offset))
            return {"items": [self._memory_dict(session, row) for row in rows],
                    "total": total, "limit": limit, "offset": offset}

    def get_memory(self, id) -> dict:
        with self.db.sessions() as session:
            return self._memory_dict(session, self._memory(session, id))

    def _index(self, session, memory):
        session.execute(text("DELETE FROM memory_fts WHERE memory_id=:id"), {"id": memory.id})
        if memory.status != "confirmed":
            return
        version = self._version(session, memory)
        evidence = self._evidence(session, version.id)
        source_ids = {e["content_id"] for e in evidence}
        sources = list(session.scalars(select(Content).where(Content.id.in_(source_ids))))
        searchable = [version.text] + [e["text"] for e in evidence]
        searchable.extend(" ".join([c.title, c.platform, c.source_url or ""]) for c in sources)
        session.execute(text("INSERT INTO memory_fts(memory_id,text) VALUES (:id,:text)"),
                        {"id": memory.id, "text": "\n".join(searchable)})

    def confirm_memory(self, id, *, expected_version=None) -> dict:
        with self.db.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            memory = self._memory(session, id)
            version = self._version(session, memory)
            if expected_version is not None and version.version != expected_version:
                raise AppError("life_fact_changed", "Memory changed. Review its current version.", 409)
            if self.life and session.get(LifeMemoryDetails, version.id):
                self.life.ensure_confirmable(session, memory, version)
                if memory.status == "inactive":
                    version = self.life.reactivate(session, memory, version)
            rows = list(session.execute(select(Content).join(ContentBlock).join(MemoryEvidence).where(
                MemoryEvidence.version_id == version.id)))
            if not rows or any(c.owner_id != memory.owner_id or c.authorship != "owner" for (c,) in rows):
                raise AppError("invalid_provenance", "Memory no longer has valid owner evidence.", 409)
            memory.status = "confirmed"
            memory.updated_at = utcnow()
            version.confirmed_at = version.confirmed_at or utcnow()
            session.flush()
            self._index(session, memory)
            self._audit(session, "memory_confirmed", memory.id, version=memory.current_version)
            return self._memory_dict(session, memory)

    def correct_memory(self, id, text, visibility=None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise AppError("invalid_fact", "Fact text must contain 1 to 10000 characters.")
        if visibility is not None:
            self._visibility(visibility)
        with self.db.sessions.begin() as session:
            from sqlalchemy import text as sql_text

            session.execute(sql_text("BEGIN IMMEDIATE"))
            memory = self._memory(session, id)
            prior = self._version(session, memory)
            life_detail = session.get(LifeMemoryDetails, prior.id)
            if life_detail and visibility == "public":
                raise AppError("life_share_required", "Use an explicit life memory share.", 409)
            block_ids = list(session.scalars(select(MemoryEvidence.block_id).where(
                MemoryEvidence.version_id == prior.id)))
            if not block_ids:
                raise AppError("invalid_provenance", "Memory no longer has source evidence.", 409)
            memory.current_version += 1
            memory.status = "candidate"
            memory.visibility = visibility or memory.visibility
            memory.updated_at = utcnow()
            version = MemoryVersion(id=new_id(), memory_id=memory.id, version=memory.current_version,
                                    text=text.strip(), kind=prior.kind, visibility=memory.visibility)
            session.add(version)
            session.flush()
            if life_detail:
                session.add(LifeMemoryDetails(version_id=version.id, topic=life_detail.topic,
                    details={**life_detail.details, "origin": "manual"},
                    source_dependencies=life_detail.source_dependencies,
                    fact_dependencies=life_detail.fact_dependencies))
            session.add_all(MemoryEvidence(version_id=version.id, block_id=b) for b in block_ids)
            session.flush()
            self._index(session, memory)
            self._audit(session, "memory_corrected", memory.id, version=memory.current_version,
                        visibility=memory.visibility)
            return self._memory_dict(session, memory)

    def deactivate_memory(self, id) -> dict:
        with self.db.sessions.begin() as session:
            memory = self._memory(session, id)
            memory.status = "inactive"
            memory.updated_at = utcnow()
            self._index(session, memory)
            self._audit(session, "memory_deactivated", memory.id, version=memory.current_version)
            return self._memory_dict(session, memory)

    def delete_memory(self, id) -> dict:
        with self.db.sessions.begin() as session:
            memory = self._memory(session, id)
            session.execute(text("DELETE FROM memory_fts WHERE memory_id=:id"), {"id": id})
            session.delete(memory)
            self._audit(session, "memory_deleted", id)
        return {"id": id, "deleted": True}

    @staticmethod
    def _terms(query):
        terms = []
        for word in re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", query.lower()):
            if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 3:
                terms.extend(word[i:i + 3] for i in range(len(word) - 2))
            else:
                terms.append(word)
        return list(dict.fromkeys(terms))[:64]

    @staticmethod
    def _index_match(terms, params):
        matches = []
        long_terms = [term for term in terms if len(term) >= 3]
        if long_terms:
            params["match"] = " OR ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
            matches.append("memory_fts.rowid IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH :match)")
        for i, term in enumerate(term for term in terms if len(term) < 3):
            params[f"term{i}"] = term
            matches.append(f"instr(lower(memory_fts.text),:term{i})>0")
        return " OR ".join(matches)

    def retrieve(self, query, scope="private", limit=8, *, purpose="local") -> list[dict]:
        self._visibility(scope)
        self._page(limit, 0)
        if not isinstance(query, str) or len(query) > 2000:
            raise AppError("invalid_query", "Query must be text containing at most 2000 characters.")
        terms = self._terms(query)
        if not terms:
            return []
        # The eligibility predicate is part of the database query, before LIMIT or evidence serialization.
        eligible = """
            m.owner_id=:owner AND m.status='confirmed' AND v.confirmed_at IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM life_memory_details ld WHERE ld.version_id=v.id)
            AND (:scope='private' OR (m.visibility='public' AND v.visibility='public'))
            AND EXISTS (SELECT 1 FROM memory_evidence e WHERE e.version_id=v.id)
            AND NOT EXISTS (
                SELECT 1 FROM memory_evidence e
                LEFT JOIN content_blocks b ON b.id=e.block_id LEFT JOIN contents c ON c.id=b.content_id
                WHERE e.version_id=v.id AND (c.id IS NULL OR c.owner_id<>:owner OR c.authorship<>'owner'
                    OR (:scope='public' AND c.visibility<>'public')))
        """
        params = {"owner": self.settings.owner_id, "scope": scope, "limit": limit}
        matches = self._index_match(terms, params)
        sql = """SELECT m.id,m.visibility,v.id AS version_id,v.text AS fact_text,m.updated_at
                 FROM memories m JOIN memory_versions v
                 ON v.memory_id=m.id AND v.version=m.current_version
                 JOIN memory_fts ON memory_fts.memory_id=m.id WHERE """
        sql += eligible + " AND (" + matches + ") ORDER BY m.updated_at DESC,m.id LIMIT :limit"
        # One SQL statement also makes filtering and evidence reads an atomic snapshot on SQLite,
        # including Python's legacy transaction mode, which otherwise does not BEGIN on SELECT.
        sql = "WITH eligible AS (" + sql + """ )
            SELECT x.id,x.visibility,x.fact_text,b.id AS block_id,b.text AS block_text,
                   c.id AS content_id,c.title,c.source_url
            FROM eligible x JOIN memory_evidence e ON e.version_id=x.version_id
            JOIN content_blocks b ON b.id=e.block_id JOIN contents c ON c.id=b.content_id
            ORDER BY x.updated_at DESC,x.id,b.content_id,b.position"""
        with self.db.sessions() as session:
            results = {}
            for row in session.execute(text(sql), params).mappings():
                entry = results.setdefault(row["id"], {"id": row["id"], "text": row["fact_text"],
                                                       "visibility": row["visibility"], "evidence": []})
                entry["evidence"].append({"block_id": row["block_id"], "content_id": row["content_id"],
                                          "title": row["title"], "source_url": row["source_url"],
                                          "text": row["block_text"]})
            ordinary = list(results.values())
        life = self.life.retrieve(query, scope, purpose, limit) if self.life else []
        return (life + ordinary)[:limit]

    def update_content_visibility(self, id, visibility) -> dict:
        self._visibility(visibility)
        with self.db.sessions.begin() as session:
            content = self._content(session, id)
            if visibility == "public" and session.get(LifeSource, id):
                raise AppError("life_share_required", "Life source text stays private.", 409)
            content.visibility = visibility
            self._audit(session, "content_visibility_updated", id, visibility=visibility)
            session.flush()
            return self._content_dict(session, content)

    def delete_content(self, id) -> dict:
        paths = []
        with self.db.sessions.begin() as session:
            self._content(session, id)
            paths = list(session.scalars(select(SourceSnapshot.raw_path).where(SourceSnapshot.content_id == id)))
            affected = list(session.scalars(select(Memory).join(MemoryVersion).join(MemoryEvidence).join(
                ContentBlock).where(ContentBlock.content_id == id)).unique())
            for memory in affected:
                memory.status = "inactive"
                memory.updated_at = utcnow()
                session.execute(text("DELETE FROM memory_fts WHERE memory_id=:id"), {"id": memory.id})
            session.execute(delete(Content).where(Content.id == id))
            self._audit(session, "content_deleted", id, affected_memory_ids=[m.id for m in affected])
        raw_root = (self.settings.data_dir.resolve() / "raw").resolve()
        cleanup_pending = False
        # Serialize reference recheck and unlink against imports across processes.
        with self.db.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for value in set(paths):
                path = Path(value).resolve()
                if not path.is_relative_to(raw_root):
                    cleanup_pending = True
                    continue
                referenced = session.scalar(select(func.count()).select_from(SourceSnapshot).where(
                    SourceSnapshot.raw_path == value))
                if not referenced:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        cleanup_pending = True
        result = {"id": id, "deleted": True}
        if cleanup_pending:
            result["raw_cleanup_pending"] = True
        return result
