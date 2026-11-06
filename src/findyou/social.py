"""Normalize read-only bili-cli results; expose only currently authorized evidence."""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from findyou.content_models import Content, SourceSnapshot
from findyou.db import new_id, utcnow
from findyou.errors import AppError
from findyou.social_models import Message, PlatformIdentity, SocialObservation, Thread, Video
from findyou.storage import Storage, _iso


def _id(value):
    if value is None or value == 0 or value == "0":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AppError("invalid_social_data", "Platform IDs must be strings or integers.")
    value = str(value).strip()
    if not value or len(value) > 200:
        raise AppError("invalid_social_data", "Platform ID is invalid.")
    return value


def _time(value=None):
    if value is None:
        return utcnow()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime):
            raise ValueError
        if result.tzinfo:
            result = result.astimezone(timezone.utc).replace(tzinfo=None)
        return result
    except (ValueError, TypeError):
        raise AppError("invalid_social_data", "Observation time must be an ISO datetime.") from None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _raw(value):
    """No nested previews or filesystem paths may bypass per-message source gates."""
    ids = {"rpid", "root", "parent", "mid", "uid", "reply_to_uid", "reply_to_mid", "aid", "oid", "cid"}
    if isinstance(value, dict):
        return {key: (_id(item) if key in ids and not isinstance(item, dict) else _raw(item))
                for key, item in value.items()
                if key not in {"raw_path", "local_path", "replies_preview", "replies"}}
    if isinstance(value, list):
        return [_raw(item) for item in value]
    return value


class SocialStore:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db

    def _entity(self, session, model, **identity):
        lookup = {k: v for k, v in identity.items() if model is not Thread or k != "video_id"}
        entity = session.scalar(select(model).where(model.owner_id == self.settings.owner_id,
                                                   model.platform == "bilibili").filter_by(**lookup))
        if entity is None:
            entity = model(id=new_id(), owner_id=self.settings.owner_id, platform="bilibili", **identity)
            session.add(entity)
            session.flush()
        return entity

    def _observe(self, session, video, content, snapshot, operation, when, payload, coverage,
                 message=None, identity=None):
        canonical = json.dumps([video.id, message.id if message else None, content.id, snapshot.id,
                                operation, _iso(when), payload, coverage], sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if session.scalar(select(SocialObservation.id).where(
                SocialObservation.owner_id == self.settings.owner_id, SocialObservation.dedup_key == digest)):
            return
        session.add(SocialObservation(id=new_id(), owner_id=self.settings.owner_id, video_id=video.id,
                                      message_id=message.id if message else None,
                                      identity_id=identity.id if identity else None,
                                      source_content_id=content.id, source_snapshot_id=snapshot.id,
                                      source_content_ref=content.id, source_snapshot_ref=snapshot.id,
                                      operation=operation, observed_at=when, dedup_key=digest,
                                      payload=payload, coverage=coverage))
        session.flush()

    def normalize(self, operation, data, video_external_id, content_id, observed_at=None, coverage=None):
        if operation not in {"video_info", "comments", "subtitle"} or not isinstance(data, dict):
            raise AppError("invalid_social_data", "Only video_info, comments and subtitle data are supported.")
        external = _id(video_external_id)
        if not external:
            raise AppError("invalid_social_data", "Video ID is required.")
        when = _time(observed_at)
        cov = {**(coverage or {}), "status": "partial", "observed_at": _iso(when)}
        cov.setdefault("requested_page", data.get("page"))
        cov.setdefault("sort", None)
        # Coverage is caller metadata, not an escape hatch for source paths or comments.
        cov = {k: v for k, v in cov.items() if k in {
            "status", "observed_at", "requested_page", "sort", "subtitle_status", "include_subtitle"}}
        with self.db.sessions.begin() as session:
            content = session.get(Content, content_id)
            if content is None or content.owner_id != self.settings.owner_id:
                raise AppError("content_not_found", "Content was not found.", 404)
            if content.authorship != "external" or content.platform != "bilibili":
                raise AppError("invalid_provenance", "Social evidence requires external Bilibili content.")
            snapshot = session.scalar(select(SourceSnapshot).where(
                SourceSnapshot.content_id == content.id).order_by(
                SourceSnapshot.captured_at.desc(), SourceSnapshot.id.desc()))
            if snapshot is None:
                raise AppError("invalid_provenance", "Social evidence requires a source snapshot.")
            video = self._entity(session, Video, external_id=external)
            thread_ids = set()
            if operation == "comments":
                rows = []

                def visit(item, enclosing=None):
                    if not isinstance(item, dict):
                        raise AppError("invalid_social_data", "Comments must be objects.")
                    rows.append((item, enclosing))
                    for child in item.get("replies_preview") or []:
                        visit(child, _id(item.get("root")) or _id(item.get("rpid")) or enclosing)

                if isinstance(data.get("root"), dict):
                    visit(data["root"])
                for item in [*(data.get("pinned") or []), *(data.get("items") or [])]:
                    visit(item)
                conflicts = 0
                candidates = {}
                rejected = set()
                for item, enclosing in rows:
                    eid = _id(item.get("rpid"))
                    if not eid:
                        continue
                    root = _id(item.get("root")) or enclosing or eid
                    parent = _id(item.get("parent"))
                    # Duplicated previews may be less complete than the same item in the root list.
                    # Prefer explicit edges, but do not select arbitrarily between contradictory edges.
                    quality = (2 if _id(item.get("root")) else 1 if enclosing else 0, bool(parent))
                    prior = candidates.get(eid)
                    if prior:
                        _old_item, old_root, old_parent, old_quality = prior
                        if ((root != old_root and quality[0] > 0 and old_quality[0] > 0)
                                or (parent and old_parent and parent != old_parent)):
                            rejected.add(eid)
                        if quality <= old_quality:
                            continue
                    candidates[eid] = (item, root, parent, quality)
                conflicts += len(rejected)
                for eid, (item, root, parent, _quality) in candidates.items():
                    if eid in rejected:
                        continue
                    # A known parent without a root is insufficient to assign a thread.
                    if root == eid and parent:
                        conflicts += 1
                        continue
                    known_parent = session.scalar(select(Message).where(
                        Message.owner_id == self.settings.owner_id, Message.platform == "bilibili",
                        Message.external_id == parent)) if parent else None
                    wrong_children = session.scalar(select(Message.id).where(
                        Message.owner_id == self.settings.owner_id, Message.platform == "bilibili",
                        Message.parent_external_id == eid,
                        (Message.root_external_id != root) | (Message.video_id != video.id)))
                    if parent == eid or wrong_children or (known_parent and (
                            known_parent.root_external_id != root or known_parent.video_id != video.id)):
                        conflicts += 1
                        continue
                    thread = self._entity(session, Thread, root_external_id=root, video_id=video.id)
                    # Global platform IDs must never be silently reassigned to another video/thread.
                    existing_thread = session.scalar(select(Thread).where(
                        Thread.owner_id == self.settings.owner_id, Thread.platform == "bilibili",
                        Thread.root_external_id == root))
                    if existing_thread.video_id != video.id:
                        conflicts += 1
                        continue
                    message = session.scalar(select(Message).where(
                        Message.owner_id == self.settings.owner_id, Message.platform == "bilibili",
                        Message.external_id == eid))
                    if message and (message.video_id != video.id or message.thread_id != thread.id
                                    or (message.parent_external_id is not None
                                        and message.parent_external_id != parent)):
                        conflicts += 1
                        continue
                    if message is None:
                        message = Message(id=new_id(), owner_id=self.settings.owner_id, platform="bilibili",
                                          video_id=video.id, thread_id=thread.id, external_id=eid,
                                          root_external_id=root, parent_external_id=parent)
                        session.add(message)
                        session.flush()
                    elif message.parent_external_id is None and parent:
                        message.parent_external_id = parent
                    user = item.get("user") or {}
                    uid = _id(user.get("mid"))
                    identity = self._entity(session, PlatformIdentity, external_id=uid) if uid else None
                    body = item.get("content")
                    if not isinstance(body, str):
                        raise AppError("invalid_social_data", "Comment content must be text.")
                    posted = item.get("time")
                    payload = {"body": body, "author_uid": uid, "author_name": user.get("name"),
                               "posted_at": _iso(_time(posted)) if posted else None,
                               "likes": _count(item.get("like")),
                               "reported_reply_count": _count(item.get("reply_count")),
                               "parent_external_id": parent, "reply_to_uid": _id(item.get("reply_to_uid")),
                               "raw": _raw(item)}
                    self._observe(session, video, content, snapshot, "message", when, payload, cov,
                                  message, identity)
                    thread_ids.add(thread.id)
                if conflicts:
                    cov["topology_conflicts"] = conflicts
            author = data.get("owner") or {}
            uid = _id(author.get("mid")) if operation == "video_info" else None
            identity = self._entity(session, PlatformIdentity, external_id=uid) if uid else None
            self._observe(session, video, content, snapshot, operation, when, _raw(data), cov, identity=identity)
            return {"video_id": video.id, "thread_ids": sorted(thread_ids), "coverage": cov}

    def _latest(self, messages=False, operation=None):
        obs = SocialObservation
        entity = obs.message_id if messages else obs.video_id
        query = select(obs.id.label("observation_id"), obs.video_id, obs.message_id, obs.identity_id,
                       obs.payload, obs.coverage, obs.source_content_id, obs.source_snapshot_id,
                       obs.observed_at, obs.recorded_at,
                       func.min(obs.observed_at).over(partition_by=entity).label("first_at"),
                       func.row_number().over(partition_by=entity, order_by=(
                           obs.observed_at.desc(), obs.recorded_at.desc(), obs.id.desc())).label("rank")).join(
            Content, Content.id == obs.source_content_id).join(
            SourceSnapshot, (SourceSnapshot.id == obs.source_snapshot_id)
            & (SourceSnapshot.content_id == Content.id)).where(
            obs.owner_id == self.settings.owner_id, Content.owner_id == self.settings.owner_id,
            Content.authorship == "external", Content.platform == "bilibili", Content.visibility == "public")
        query = query.where(obs.operation == "message" if messages else obs.message_id.is_(None))
        if operation:
            query = query.where(obs.operation == operation)
        ranked = query.subquery()
        return select(ranked).where(ranked.c.rank == 1).subquery()

    def _videos(self):
        latest = self._latest()
        return select(Video, latest).join(latest, latest.c.video_id == Video.id).where(
            Video.owner_id == self.settings.owner_id)

    def _messages(self):
        latest = self._latest(messages=True)
        return select(Message, latest).join(latest, latest.c.message_id == Message.id).where(
            Message.owner_id == self.settings.owner_id)

    @staticmethod
    def _sort(query, column, sort, tie):
        if sort not in {"observed_desc", "observed_asc"}:
            raise AppError("invalid_sort", "Unknown social ordering.")
        return query.order_by(column.desc() if sort == "observed_desc" else column.asc(), tie)

    @staticmethod
    def _q(query, q, *columns):
        if q is None:
            return query
        if not isinstance(q, str) or len(q) > 2000:
            raise AppError("invalid_query", "Query must contain at most 2000 characters.")
        from sqlalchemy import or_
        return query.where(or_(*(func.instr(func.lower(c), q.lower()) > 0 for c in columns)))

    def _video_dict(self, session, row):
        video = row[0]
        info = self._latest(operation="video_info")
        info_row = session.execute(select(info).where(info.c.video_id == video.id)).first()
        data = info_row.payload if info_row else {}
        author = data.get("owner") or {}
        messages = self._messages().where(Message.video_id == video.id).subquery()
        counts = session.execute(select(func.count(), func.count(func.distinct(messages.c.thread_id)),
                                        func.count(func.distinct(messages.c.payload["author_uid"].as_string())))
                                 .select_from(messages)).one()
        comments = self._latest(operation="comments")
        comment_data = session.scalar(select(comments.c.payload).where(comments.c.video_id == video.id)) or {}
        subtitles = self._latest(operation="subtitle")
        subtitle = session.execute(select(subtitles).where(subtitles.c.video_id == video.id)).first()
        subtitle_text = None
        if subtitle:
            subtitle_text = "\n".join(item["content"] for item in subtitle.payload.get("items", [])
                                      if isinstance(item, dict) and isinstance(item.get("content"), str)) or None
            if subtitle_text is None and isinstance(subtitle.payload.get("text"), str):
                subtitle_text = subtitle.payload["text"] or None
        subtitle_status = subtitle.coverage.get("subtitle_status", "collected") if subtitle else None
        if subtitle is None and row.coverage.get("subtitle_status") in {
                "not_requested", "unavailable", "not_found", "failed", "not_collected"}:
            subtitle_status = row.coverage["subtitle_status"]
        visible_coverage = dict(row.coverage)
        if "subtitle_status" in visible_coverage:
            visible_coverage["subtitle_status"] = subtitle_status
        return {"id": video.id, "platform": video.platform, "external_id": video.external_id,
                "title": data.get("title"), "author_name": author.get("name"),
                "author_uid": _id(author.get("mid")), "source_url": data.get("url") or (
                    "https://www.bilibili.com/video/" + video.external_id), "description": data.get("desc"),
                "reported_comment_count": _count((data.get("stat") or {}).get("reply"))
                if "reply" in (data.get("stat") or {}) else _count(comment_data.get("total")),
                "collected_message_count": counts[0], "collected_thread_count": counts[1],
                "participant_count": counts[2], "last_observed_at": _iso(row.observed_at),
                "coverage": visible_coverage,
                "metadata_source_content_id": info_row.source_content_id if info_row else None,
                "metadata_source_snapshot_id": info_row.source_snapshot_id if info_row else None,
                "subtitle_status": subtitle_status, "subtitle_text": subtitle_text,
                "subtitle_source_content_id": subtitle.source_content_id if subtitle else None,
                "subtitle_source_snapshot_id": subtitle.source_snapshot_id if subtitle else None}

    def _get_video(self, session, id):
        row = session.execute(self._videos().where(Video.id == id)).first()
        if row is None:
            raise AppError("video_not_found", "Video was not found.", 404)
        return self._video_dict(session, row)

    def get_video(self, id):
        with self.db.sessions() as session:
            return self._get_video(session, id)

    def list_videos(self, limit=20, offset=0, q=None, sort="observed_desc"):
        Storage._page(limit, offset)
        latest, info = self._latest(), self._latest(operation="video_info")
        query = select(Video, latest).join(latest, latest.c.video_id == Video.id).outerjoin(
            info, info.c.video_id == Video.id).where(Video.owner_id == self.settings.owner_id)
        query = self._q(query, q, Video.external_id, info.c.payload["title"].as_string(),
                        info.c.payload["desc"].as_string())
        query = self._sort(query, latest.c.observed_at, sort, Video.id)
        with self.db.sessions() as session:
            total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
            return {"items": [self._video_dict(session, row) for row in session.execute(
                query.limit(limit).offset(offset))], "total": total, "limit": limit, "offset": offset}

    def _threads(self):
        latest = self._latest(messages=True)
        stats = select(Message.thread_id, func.max(latest.c.observed_at).label("last_at"),
                       func.count().label("message_count"),
                       func.count(func.distinct(latest.c.payload["author_uid"].as_string())).label("participants"))\
            .join(latest, latest.c.message_id == Message.id).where(Message.owner_id == self.settings.owner_id)\
            .group_by(Message.thread_id).subquery()
        return select(Thread, stats).join(stats, stats.c.thread_id == Thread.id).where(
            Thread.owner_id == self.settings.owner_id)

    def _message_dict(self, session, row):
        message, data = row[0], row.payload
        parent = data.get("parent_external_id")
        parent_exists = parent and session.execute(self._messages().where(
            Message.thread_id == message.thread_id, Message.external_id == parent)).first() is not None
        return {"id": message.id, "video_id": message.video_id, "thread_id": message.thread_id,
                "external_id": message.external_id, "root_external_id": message.root_external_id,
                **{k: data.get(k) for k in ("parent_external_id", "reply_to_uid", "author_uid", "author_name",
                                           "body", "posted_at", "likes", "reported_reply_count", "raw")},
                "first_observed_at": _iso(row.first_at), "last_observed_at": _iso(row.observed_at),
                "source_content_id": row.source_content_id, "source_snapshot_id": row.source_snapshot_id,
                "is_root": message.external_id == message.root_external_id,
                "parent_missing": message.external_id != message.root_external_id and not bool(parent_exists)}

    def _thread_dict(self, session, row):
        thread = row[0]
        root_row = session.execute(self._messages().where(Message.thread_id == thread.id,
                                                        Message.external_id == thread.root_external_id)).first()
        root = self._message_dict(session, root_row) if root_row else None
        latest = self._latest(messages=True)
        current = session.execute(select(latest.c.coverage).join(
            Message, Message.id == latest.c.message_id).where(Message.thread_id == thread.id,
            Message.owner_id == self.settings.owner_id).order_by(
            latest.c.observed_at.desc(), latest.c.recorded_at.desc(), latest.c.observation_id.desc()).limit(1)).one()
        return {"id": thread.id, "video_id": thread.video_id, "root_external_id": thread.root_external_id,
                "root_message_id": root["id"] if root else None, "title": root["body"][:120] if root else None,
                "author_name": root["author_name"] if root else None,
                "author_uid": root["author_uid"] if root else None,
                "reported_reply_count": root["reported_reply_count"] if root else None,
                "collected_message_count": row.message_count, "participant_count": row.participants,
                "last_observed_at": _iso(row.last_at), "coverage": current.coverage}

    def _get_thread(self, session, id, detail=False):
        row = session.execute(self._threads().where(Thread.id == id)).first()
        if row is None:
            raise AppError("thread_not_found", "Thread was not found.", 404)
        result = self._thread_dict(session, row)
        if detail:
            result["video"] = self._get_video(session, row[0].video_id)
            root = session.execute(self._messages().where(Message.thread_id == id,
                                                          Message.external_id == row[0].root_external_id)).first()
            result["root_message"] = self._message_dict(session, root) if root else None
        return result

    def get_thread(self, id):
        with self.db.sessions() as session:
            return self._get_thread(session, id, True)

    def list_threads(self, limit=20, offset=0, video_id=None, q=None, sort="observed_desc"):
        Storage._page(limit, offset)
        with self.db.sessions() as session:
            if video_id:
                self._get_video(session, video_id)
            query = self._threads()
            if video_id:
                query = query.where(Thread.video_id == video_id)
            if q is not None:
                latest = self._latest(messages=True)
                matching = select(Message.thread_id).join(latest, latest.c.message_id == Message.id).where(
                    Message.owner_id == self.settings.owner_id)
                matching = self._q(matching, q, latest.c.payload["body"].as_string(),
                                   latest.c.payload["author_name"].as_string(), Message.external_id)
                query = query.where(Thread.id.in_(matching))
            query = self._sort(query, query.selected_columns.last_at, sort, Thread.id)
            total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
            return {"items": [self._thread_dict(session, row) for row in session.execute(
                query.limit(limit).offset(offset))], "total": total, "limit": limit, "offset": offset}

    def list_messages(self, thread_id, limit=50, offset=0, parent_id=None):
        Storage._page(limit, offset)
        with self.db.sessions() as session:
            self._get_thread(session, thread_id)
            latest = self._latest(messages=True)
            query = select(Message, latest).join(latest, latest.c.message_id == Message.id).where(
                Message.owner_id == self.settings.owner_id, Message.thread_id == thread_id)
            if parent_id is not None:
                parent = _id(parent_id)
                if parent is None:
                    raise AppError("invalid_social_data", "Parent ID must be a platform message ID.")
                query = query.where(latest.c.payload["parent_external_id"].as_string() == parent)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            query = query.order_by(latest.c.observed_at, Message.external_id, Message.id)
            return {"items": [self._message_dict(session, row) for row in session.execute(
                query.limit(limit).offset(offset))], "total": total, "limit": limit, "offset": offset}

    def get_message(self, id):
        with self.db.sessions() as session:
            row = session.execute(self._messages().where(Message.id == id)).first()
            if row is None:
                raise AppError("message_not_found", "Message was not found.", 404)
            return {**self._message_dict(session, row), "video": self._get_video(session, row[0].video_id),
                    "thread": self._get_thread(session, row[0].thread_id, True)}

    def summary(self):
        with self.db.sessions() as session:
            latest = self._latest(messages=True)
            info = self._latest(operation="video_info")
            identity_ids = select(latest.c.identity_id).where(latest.c.identity_id.is_not(None)).union(
                select(info.c.identity_id).where(info.c.identity_id.is_not(None))).subquery()
            return {"videos": session.scalar(select(func.count()).select_from(self._videos().subquery())),
                    "threads": session.scalar(select(func.count()).select_from(self._threads().subquery())),
                    "messages": session.scalar(select(func.count()).select_from(self._messages().subquery())),
                    "identities": session.scalar(select(func.count()).select_from(identity_ids)),
                    "platform_write": False}
