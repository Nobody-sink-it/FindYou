"""Synthetic platform fixtures only: never copy real users or comments into this file."""

import pytest
from sqlalchemy import delete, func, select

from findyou.config import Settings
from findyou.content_models import Content, Owner, SourceSnapshot
from findyou.db import Base, Database, new_id
from findyou.errors import AppError
from findyou.social import SocialStore
from findyou.social_models import Message, PlatformIdentity, SocialObservation, Thread, Video

T1 = "2026-01-01T00:00:00Z"
T2 = "2026-01-02T00:00:00Z"
BV = "BV0000000001"
BIG = "98765432109876543210"


@pytest.fixture
def store(tmp_path):
    settings = Settings(data_dir=tmp_path, sqlite_journal_mode="DELETE", _env_file=None)
    db = Database(settings)
    Base.metadata.create_all(db.engine)
    with db.sessions.begin() as session:
        session.add_all([Owner(id=settings.owner_id, name="test"), Owner(id="other", name="other")])
    yield SocialStore(settings, db)
    db.engine.dispose()


def source(store, owner=None, visibility="public", authorship="external", snapshots=True):
    identifier = new_id()
    with store.db.sessions.begin() as session:
        session.add(Content(id=identifier, owner_id=owner or store.settings.owner_id, title="fixture",
                            platform="bilibili", authorship=authorship, visibility=visibility,
                            format="json", sha256=identifier.replace("-", "") * 2, metadata_json={}))
        session.flush()
        if snapshots:
            session.add(SourceSnapshot(content_id=identifier, sha256="a" * 64,
                                       raw_path="/synthetic/no-public-path.json", format="json"))
    return identifier


def comment(id=BIG, root=None, parent=None, body="Synthetic root", uid="12345678901234567890", **extra):
    return {"rpid": id, "root": root, "parent": parent, "content": body,
            "user": {"mid": uid, "name": "Fixture author"}, "like": 5, "reply_count": 2,
            "time": "2025-12-01T08:00:00+08:00", **extra}


def capture(store, items=None, content=None, when=T1, video=BV, **extra):
    return store.normalize("comments", {"items": items if items is not None else [comment()],
                                         "page": 1, "total": 30, **extra}, video,
                           content or source(store), when, {"sort": "hot"})


def messages(store, result):
    return store.list_messages(result["thread_ids"][0])["items"]


def revoke(store, id, deletion=False):
    with store.db.sessions.begin() as session:
        if deletion:
            session.execute(delete(Content).where(Content.id == id))
        else:
            session.get(Content, id).visibility = "private"


def test_idempotent_preview_pinned_and_large_string_ids(store):
    src = source(store)
    child = comment(int(BIG) + 1, root=int(BIG), parent=int(BIG), body="Synthetic reply", uid=99)
    root = comment(int(BIG), replies_preview=[child])
    data = {"items": [root, child], "pinned": [root], "total": 30, "page": 1}
    result = store.normalize("comments", data, BV, src, T1)
    assert store.normalize("comments", data, BV, src, T1) == result
    page = store.list_messages(result["thread_ids"][0])
    assert page["total"] == 2
    assert {x["external_id"] for x in page["items"]} == {BIG, str(int(BIG) + 1)}
    reply = next(x for x in page["items"] if not x["is_root"])
    assert reply["parent_external_id"] == BIG and reply["parent_missing"] is False
    assert reply["reply_to_uid"] is None  # A parent is not evidence of an explicit reply-to UID.
    assert reply["raw"]["rpid"] == str(int(BIG) + 1)
    assert next(x for x in page["items"] if x["is_root"])["posted_at"] == "2025-12-01T00:00:00.000000Z"
    assert "replies_preview" not in next(x for x in page["items"] if x["is_root"])["raw"]
    assert store.summary() == {"videos": 1, "threads": 1, "messages": 2, "identities": 2,
                               "platform_write": False}
    with store.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SocialObservation)) == 3
        assert session.scalar(select(func.count()).select_from(Video)) == 1
        assert session.scalar(select(func.count()).select_from(Thread)) == 1
        assert session.scalar(select(func.count()).select_from(Message)) == 2
        assert session.scalar(select(func.count()).select_from(PlatformIdentity)) == 2


def test_missing_parent_and_later_parent_arrival(store):
    child = comment("child", root="root", parent="missing", body="child")
    result = capture(store, [child])
    assert messages(store, result)[0]["parent_missing"] is True
    thread = store.get_thread(result["thread_ids"][0])
    assert thread["root_message"] is None and thread["root_message_id"] is None
    capture(store, [comment("missing", root="root", parent="root")], when=T2)
    assert next(x for x in messages(store, result) if x["external_id"] == "child")["parent_missing"] is False
    # No root content was inferred from a child preview.
    assert store.get_thread(thread["id"])["root_message"] is None


def test_preview_enclosure_is_evidence_for_root_but_not_parent(store):
    result = capture(store, [comment(replies_preview=[comment("child", body="child")])])
    child = next(x for x in messages(store, result) if x["external_id"] == "child")
    assert child["root_external_id"] == BIG and child["parent_external_id"] is None
    assert child["parent_missing"] is True


def test_updates_keep_history_missing_from_page_is_not_deletion(store):
    first = source(store)
    result = capture(store, [comment(), comment("second")], content=first)
    root = next(x for tid in result["thread_ids"] for x in store.list_messages(tid)["items"]
                if x["external_id"] == BIG)
    second = source(store)
    capture(store, [comment(body="Edited synthetic text", like=9)], content=second, when=T2)
    current = store.get_message(root["id"])
    assert current["body"] == "Edited synthetic text" and current["likes"] == 9
    assert current["first_observed_at"].startswith("2026-01-01")
    assert current["last_observed_at"].startswith("2026-01-02")
    assert current["source_content_id"] == second
    assert current["video"]["id"] == result["video_id"] and current["thread"]["id"] == root["thread_id"]
    assert store.summary()["messages"] == 2
    with store.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SocialObservation).where(
            SocialObservation.message_id == root["id"])) == 2


@pytest.mark.parametrize("deletion", [False, True])
def test_revoke_latest_source_falls_back_then_hides_all_queries(store, deletion):
    old, new = source(store), source(store)
    result = capture(store, content=old)
    msg = messages(store, result)[0]
    capture(store, [comment(body="Revoked update")], content=new, when=T2)
    revoke(store, new, deletion)
    current = store.get_message(msg["id"])
    assert current["body"] == "Synthetic root" and current["source_content_id"] == old
    assert store.list_videos(q="Revoked")["total"] == 0
    assert store.list_threads(q="Revoked")["total"] == 0
    revoke(store, old, deletion)
    assert store.list_videos()["total"] == store.list_threads()["total"] == 0
    assert store.summary() == {"videos": 0, "threads": 0, "messages": 0, "identities": 0,
                               "platform_write": False}
    for call, id in [(store.get_video, result["video_id"]), (store.get_thread, msg["thread_id"]),
                     (store.get_message, msg["id"]), (store.list_messages, msg["thread_id"])]:
        with pytest.raises(AppError) as exc:
            call(id)
        assert exc.value.status == 404
    with store.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SocialObservation)) == 4
        observations = list(session.scalars(select(SocialObservation)))
        assert {o.source_content_ref for o in observations} == {old, new}
        assert all(o.source_snapshot_ref for o in observations)
        if deletion:
            assert all(o.source_content_id is None and o.source_snapshot_id is None for o in observations)


def test_video_metadata_sources_revoked_independently_of_comments(store):
    info_src = source(store)
    info = {"bvid": BV, "title": "Synthetic title", "desc": "Synthetic description",
            "owner": {"mid": 42, "name": "Fixture uploader"}, "stat": {"reply": 80}}
    result = store.normalize("video_info", info, BV, info_src, T1)
    assert store.get_video(result["video_id"])["collected_message_count"] == 0
    comments_src = source(store)
    capture(store, content=comments_src, when=T2)
    revoke(store, info_src)
    video = store.get_video(result["video_id"])
    assert video["title"] is None and video["author_uid"] is None and video["description"] is None
    assert video["metadata_source_content_id"] is None and video["metadata_source_snapshot_id"] is None
    assert video["reported_comment_count"] == 30
    assert store.list_videos(q="Synthetic title")["total"] == 0
    revoke(store, comments_src)
    with pytest.raises(AppError):
        store.get_video(result["video_id"])


def test_info_only_remains_visible_without_revoked_messages(store):
    result = store.normalize("video_info", {"title": "Only info"}, BV, source(store), T1)
    src = source(store)
    comments = capture(store, content=src)
    msg = messages(store, comments)[0]
    revoke(store, src)
    video = store.get_video(result["video_id"])
    assert video["title"] == "Only info" and video["collected_message_count"] == 0
    assert video["collected_thread_count"] == 0 and video["participant_count"] == 0
    assert store.summary()["threads"] == store.summary()["messages"] == 0
    with pytest.raises(AppError):
        store.get_message(msg["id"])


def test_owner_isolation_and_provenance_requirements(store):
    result = capture(store)
    msg = messages(store, result)[0]
    other = SocialStore(store.settings.model_copy(update={"owner_id": "other"}), store.db)
    assert other.list_videos()["total"] == other.list_threads()["total"] == 0
    assert other.summary()["identities"] == 0
    for call, id in [(other.get_video, result["video_id"]), (other.get_thread, msg["thread_id"]),
                     (other.get_message, msg["id"]), (other.list_messages, msg["thread_id"])]:
        with pytest.raises(AppError):
            call(id)
    for src in ["missing", source(store, owner="other"), source(store, authorship="owner"),
                source(store, snapshots=False)]:
        with pytest.raises(AppError):
            capture(store, content=src)
    own = capture(other)
    assert own["video_id"] != result["video_id"]
    assert other.summary()["messages"] == 1


def test_private_source_is_stored_but_never_public(store):
    src = source(store, visibility="private")
    result = capture(store, content=src)
    assert store.summary()["messages"] == 0
    with pytest.raises(AppError):
        store.get_video(result["video_id"])
    with store.db.sessions.begin() as session:
        session.get(Content, src).visibility = "public"
    assert store.summary()["messages"] == 1


def test_parent_and_message_cannot_be_moved_between_threads_or_videos(store):
    first = capture(store)
    capture(store, [comment("root2")])
    bad_parent = capture(store, [comment("bad", root="root2", parent=BIG)])
    assert bad_parent["coverage"]["topology_conflicts"] == 1
    bad_move = capture(store, [comment(BIG, root="root2", parent="root2")])
    assert bad_move["coverage"]["topology_conflicts"] == 1
    bad_video = capture(store, video="BV0000000002")
    assert bad_video["coverage"]["topology_conflicts"] == 1
    assert messages(store, first)[0]["body"] == "Synthetic root"
    assert store.summary()["messages"] == 2


def test_late_parent_cannot_contradict_existing_child_thread(store):
    capture(store, [comment("child", root="root1", parent="late")])
    result = capture(store, [comment("late", root="root2", parent="root2")])
    assert result["coverage"]["topology_conflicts"] == 1
    assert store.summary()["messages"] == 1


def test_pagination_search_sort_filter_and_validation(store):
    a = capture(store)
    capture(store, [comment("otherroot", body="needle")], video="BV0000000002", when=T2)
    assert store.list_videos(limit=1)["items"][0]["external_id"] == "BV0000000002"
    assert store.list_videos(limit=1, offset=1)["items"][0]["id"] == a["video_id"]
    assert store.list_videos(sort="observed_asc")["items"][0]["id"] == a["video_id"]
    assert store.list_threads(q="needle")["total"] == 1
    assert store.list_threads(q="' OR 1=1 --")["total"] == 0
    assert store.list_threads(video_id=a["video_id"])["total"] == 1
    assert store.list_threads(sort="observed_asc")["items"][0]["video_id"] == a["video_id"]
    capture(store, [comment("child", root=BIG, parent=BIG)])
    assert store.list_messages(a["thread_ids"][0], parent_id=BIG)["total"] == 1
    assert store.list_messages(a["thread_ids"][0], limit=1, offset=1)["total"] == 2
    for kwargs in [{"limit": 0}, {"limit": True}, {"offset": -1}, {"sort": "sql"}]:
        with pytest.raises(AppError):
            store.list_videos(**kwargs)


def test_subtitle_source_is_retained_and_public_output_has_no_raw_paths(store):
    src = source(store)
    data = {"bvid": BV, "page": 1, "lang": "zh", "items": [{"from": 0, "to": 2,
                                                                          "content": "Synthetic subtitle"}]}
    result = store.normalize("subtitle", data, BV, src, T1, {"subtitle_status": "collected",
                                                            "raw_path": "/secret"})
    video = store.get_video(result["video_id"])
    assert video["coverage"]["subtitle_status"] == "collected"
    assert "raw_path" not in str(video) and video["subtitle_text"] == "Synthetic subtitle"
    assert video["subtitle_source_content_id"] == src and video["subtitle_source_snapshot_id"]
    with store.db.sessions() as session:
        obs = session.scalar(select(SocialObservation))
        assert obs.source_content_id == src and obs.source_snapshot_id
        assert obs.payload["items"][0]["content"] == "Synthetic subtitle"


def test_invalid_ids_do_not_round_floats_and_import_is_atomic(store):
    src = source(store)
    with pytest.raises(AppError):
        capture(store, [comment(), comment(9007199254740992.0)], content=src)
    assert store.summary()["messages"] == 0
    with store.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 0


def test_duplicate_preview_uses_explicit_parent_without_extra_observations(store):
    preview = comment("child", root=BIG, body="child")
    listed = comment("child", root=BIG, parent=BIG, body="child")
    result = capture(store, [comment(replies_preview=[preview]), listed])
    child = next(x for x in messages(store, result) if x["external_id"] == "child")
    assert child["parent_external_id"] == BIG and child["parent_missing"] is False
    with store.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SocialObservation).where(
            SocialObservation.message_id == child["id"])) == 1


def test_conflicting_duplicate_edges_are_not_arbitrarily_selected(store):
    result = capture(store, [comment(replies_preview=[comment("child", root=BIG, parent=BIG)]),
                             comment("child", root="different", parent="different")])
    assert result["coverage"]["topology_conflicts"] == 1
    assert store.summary()["messages"] == 1
    assert messages(store, result)[0]["external_id"] == BIG


def test_out_of_order_and_equal_time_observations_are_deterministic(store):
    newest = capture(store, [comment(body="newest")], when=T2)
    msg = messages(store, newest)[0]
    capture(store, [comment(body="older")], when=T1)
    assert store.get_message(msg["id"])["body"] == "newest"
    capture(store, [comment(body="same time newer capture")], when=T2)
    assert store.get_message(msg["id"])["body"] == "same time newer capture"


def test_video_metadata_falls_back_to_old_public_source(store):
    old, new = source(store), source(store)
    first = store.normalize("video_info", {"title": "Old public metadata", "owner": {"mid": 1}}, BV, old, T1)
    store.normalize("video_info", {"title": "Revoked metadata", "owner": {"mid": 2}}, BV, new, T2)
    revoke(store, new)
    video = store.get_video(first["video_id"])
    assert video["title"] == "Old public metadata" and video["author_uid"] == "1"
    assert video["metadata_source_content_id"] == old and video["metadata_source_snapshot_id"]
    assert store.list_videos(q="Old public")["total"] == 1
    assert store.list_videos(q="Revoked")["total"] == 0
    assert store.summary()["identities"] == 1


def test_subtitle_text_and_refs_are_gated_independently(store):
    video = store.normalize("video_info", {"title": "info"}, BV, source(store), T1)
    before = store.get_video(video["video_id"])
    assert before["subtitle_text"] is None and before["subtitle_status"] is None
    old, new = source(store), source(store)
    store.normalize("subtitle", {"items": [{"content": "old subtitle"}]}, BV, old, T1)
    store.normalize("subtitle", {"items": [{"content": "new subtitle"}]}, BV, new, T2)
    assert store.get_video(video["video_id"])["subtitle_text"] == "new subtitle"
    revoke(store, new)
    current = store.get_video(video["video_id"])
    assert current["subtitle_text"] == "old subtitle" and current["subtitle_source_content_id"] == old
    revoke(store, old, deletion=True)
    current = store.get_video(video["video_id"])
    assert current["subtitle_text"] is None and current["subtitle_status"] is None
    assert current["subtitle_source_content_id"] is None and current["subtitle_source_snapshot_id"] is None
