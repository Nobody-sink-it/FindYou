import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select, text

from findyou.config import Settings
from findyou.content_models import MemoryEvidence, MemoryVersion
from findyou.contracts import ImportRequest
from findyou.db import SCHEMA_HEAD, Database
from findyou.errors import AppError
from findyou.ops import AuditEvent
from findyou.storage import Storage


@pytest.fixture
def store(tmp_path):
    settings = Settings(data_dir=tmp_path, sqlite_journal_mode="DELETE", llm_provider="fake", _env_file=None)
    db = Database(settings)
    db.initialize()
    yield Storage(settings, db)
    db.engine.dispose()


def imported(store, text="我喜欢量子物理，也喜欢茶。", **kwargs):
    return store.import_content(ImportRequest(text=text, **kwargs))


def proposed(store, content=None, fact="我喜欢物理"):
    content = content or imported(store)
    return store.propose_facts(content["id"], [{"text": fact, "evidence_block_ids": [content["blocks"][0]["id"]]}])[0]


def confirmed(store, content=None, fact="我喜欢物理"):
    return store.confirm_memory(proposed(store, content, fact)["id"])


def test_initialization_is_idempotent_and_foreign_keys_active(store):
    store.db.initialize()
    state = store.db.readiness()
    assert state == {"ready": True, "sqlite_version": sqlite3.sqlite_version,
                     "journal_mode": "delete", "schema_revision": SCHEMA_HEAD}
    with store.db.engine.connect() as conn:
        assert conn.scalar(text("PRAGMA foreign_keys")) == 1
        assert conn.scalar(text("PRAGMA busy_timeout")) == 10000
        assert conn.scalar(text("SELECT count(*) FROM owners")) == 1
        assert conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='jobs'")) == 1
        assert "trigram" in conn.scalar(text("SELECT sql FROM sqlite_master WHERE name='memory_fts'"))


def test_wal_version_gate_uses_actual_loaded_library(tmp_path):
    db = Database(Settings(data_dir=tmp_path, sqlite_journal_mode="WAL", _env_file=None))
    if tuple(map(int, sqlite3.sqlite_version.split("."))) < (3, 51, 3):
        with pytest.raises(AppError, match="WAL requires SQLite >=3.51.3"):
            db.initialize()
        assert db.readiness()["ready"] is False
        with sqlite3.connect(tmp_path / "findyou.db") as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
    else:
        db.initialize()
        assert db.readiness()["journal_mode"] == "wal"
    db.engine.dispose()


def test_snapshot_dedup_is_immutable_and_segregated(store):
    first = imported(store, title="日记", visibility="private", source_url="https://example.invalid/note")
    repeat = imported(store, title="promote?", visibility="public", source_url="https://example.invalid/other")
    assert repeat["duplicate"] is True
    assert repeat["id"] == first["id"]
    assert repeat["visibility"] == "private" and repeat["title"] == "日记"
    assert repeat["source_url"] == first["source_url"]
    raw = Path(first["snapshots"][0]["raw_path"])
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == first["sha256"]
    external = imported(store, authorship="external")
    platform = imported(store, platform="bilibili")
    assert len({first["id"], external["id"], platform["id"]}) == 3
    assert store.list_content()["total"] == 3
    assert json.loads(json.dumps(first, ensure_ascii=False))["blocks"]


def test_multiline_blocks_preserve_offsets_and_snapshot(store):
    raw = "第一行\n第二行\n\n" + "中文" * 2000
    content = imported(store, raw, format="markdown")
    assert len(content["blocks"]) == 4
    for block in content["blocks"]:
        locator = block["locator"]
        assert raw[locator["start"]:locator["end"]] == block["text"]
        assert block["snapshot_id"] == content["snapshots"][0]["id"]


def test_provenance_validation_is_atomic(store):
    source = imported(store)
    foreign = imported(store, "这是另一份材料")
    cases = [[], ["missing"], [foreign["blocks"][0]["id"]], [123]]
    for evidence in cases:
        with pytest.raises(AppError) as err:
            store.propose_facts(source["id"], [{"text": "偏好", "evidence_block_ids": evidence}])
        assert err.value.code == "invalid_provenance"
    valid = {"text": "有效事实", "evidence_block_ids": [source["blocks"][0]["id"]]}
    with pytest.raises(AppError):
        store.propose_facts(source["id"], [valid, {"text": "bad", "evidence_block_ids": []}])
    assert store.list_memories()["total"] == 0
    with pytest.raises(AppError) as err:
        proposed(store, imported(store, "别人的话", authorship="external"))
    assert err.value.code == "external_owner_memory_forbidden"


def test_fact_lifecycle_preserves_versions_and_invalidates_approval(store):
    memory = proposed(store)
    assert memory["status"] == "candidate"
    assert store.retrieve("物理") == []
    memory = store.confirm_memory(memory["id"])
    assert len(store.retrieve("量子物理")) == 1
    old_evidence = memory["evidence"]
    memory = store.correct_memory(memory["id"], "我现在偏爱天文学", visibility="public")
    assert memory["status"] == "candidate" and memory["current_version"] == 2
    assert memory["versions"][0]["confirmed_at"]
    assert memory["versions"][1]["confirmed_at"] is None
    assert memory["evidence"] == old_evidence
    assert store.retrieve("天文学") == []
    store.confirm_memory(memory["id"])
    assert store.retrieve("天文学")[0]["text"] == "我现在偏爱天文学"
    assert store.retrieve("天文学", "public") == []  # source is still private
    store.deactivate_memory(memory["id"])
    assert store.retrieve("天文学") == []
    store.delete_memory(memory["id"])
    with pytest.raises(AppError) as err:
        store.get_memory(memory["id"])
    assert err.value.status == 404
    with store.db.sessions() as session:
        assert not list(session.scalars(select(MemoryVersion)))
        assert not list(session.scalars(select(MemoryEvidence)))
        assert session.scalar(text("SELECT count(*) FROM memory_fts")) == 0


def test_fts_trigrams_short_chinese_and_source_fields(store):
    memory = confirmed(store, imported(store, title="登山运动", text="我每周去户外。"), fact="我常运动")
    assert store.retrieve("登山运动")[0]["id"] == memory["id"]
    assert store.retrieve("山")[0]["id"] == memory["id"]
    assert store.retrieve("户外")[0]["id"] == memory["id"]
    assert store.retrieve("我平时喜欢登山运动吗")[0]["id"] == memory["id"]
    assert store.retrieve('" OR * ; DROP TABLE memories; --') == []
    assert store.retrieve("  ") == []


def test_public_eligibility_precedes_limit_and_tracks_visibility(store):
    public = confirmed(store, imported(store, "我热爱园艺", visibility="public"), fact="我喜欢园艺")
    for i in range(3):
        confirmed(store, imported(store, f"园艺秘密{i}"), fact="园艺偏好")
    assert store.retrieve("园艺", "public", 1)[0]["id"] == public["id"]
    source_id = public["evidence"][0]["content_id"]
    store.update_content_visibility(source_id, "private")
    assert store.retrieve("园艺", "public") == []
    store.update_content_visibility(source_id, "public")
    output = store.retrieve("园艺", "public")
    assert len(output) == 1
    assert set(output[0]) == {"id", "text", "visibility", "evidence"}
    assert set(output[0]["evidence"][0]) == {"block_id", "content_id", "title", "source_url", "text"}


def test_all_evidence_must_be_public_and_owner_eligible(store):
    public = confirmed(store, imported(store, "公开喜好", visibility="public"), fact="喜欢公开")
    secret = imported(store, "秘密喜好")
    with store.db.sessions.begin() as session:
        version = session.scalar(select(MemoryVersion).where(MemoryVersion.memory_id == public["id"]))
        session.add(MemoryEvidence(version_id=version.id, block_id=secret["blocks"][0]["id"]))
    assert store.retrieve("公开", "public") == []
    assert store.retrieve("公开", "private")


def test_owner_boundaries(store):
    private = imported(store, "我的唯一机密")
    confirmed(store, private, fact="我的唯一机密")
    other_settings = store.settings.model_copy(update={"owner_id": "another-owner"})
    other_db = Database(other_settings)
    other_db.initialize()
    other = Storage(other_settings, other_db)
    try:
        assert other.list_content()["total"] == 0
        assert other.list_memories()["total"] == 0
        assert other.retrieve("唯一机密") == []
        with pytest.raises(AppError) as err:
            other.get_content(private["id"])
        assert err.value.status == 404
        other_content = imported(other, "我的唯一机密")
        assert other_content["id"] != private["id"]
        store.delete_content(private["id"])
        assert Path(other_content["snapshots"][0]["raw_path"]).exists()
    finally:
        other_db.engine.dispose()


def test_content_delete_invalidates_facts_and_removes_unreferenced_raw(store):
    content = imported(store)
    memory = confirmed(store, content)
    store.correct_memory(memory["id"], "我也喜欢茶")
    store.confirm_memory(memory["id"])
    raw = Path(content["snapshots"][0]["raw_path"])
    assert raw.exists()
    store.delete_content(content["id"])
    assert not raw.exists()
    assert store.retrieve("喜欢") == []
    assert store.get_memory(memory["id"])["status"] == "inactive"
    assert store.get_memory(memory["id"])["evidence"] == []
    with pytest.raises(AppError):
        store.confirm_memory(memory["id"])
    with store.db.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM content_blocks")) == 0
        assert conn.scalar(text("SELECT count(*) FROM source_snapshots")) == 0
        assert conn.scalar(text("SELECT count(*) FROM memory_fts")) == 0


def test_explicit_validation_and_not_found(store):
    for call in [lambda: store.list_content(0), lambda: store.list_memories(offset=-1),
                 lambda: store.retrieve("hi", "invalid"), lambda: store.list_memories(status="deleted"),
                 lambda: imported(store, "   "), lambda: store.correct_memory("missing", " "),
                 lambda: store.update_content_visibility("missing", "world")]:
        with pytest.raises(AppError):
            call()


def test_concurrent_duplicate_imports_are_atomic(store):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: imported(store, "并发导入的唯一内容"), range(8)))
    assert len({r["id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1
    content = store.get_content(results[0]["id"])
    assert len(content["blocks"]) == len(content["snapshots"]) == 1
    assert Path(content["snapshots"][0]["raw_path"]).read_text(encoding="utf-8") == "并发导入的唯一内容"


def test_raw_corruption_rejects_new_reference(store):
    original = imported(store)
    path = Path(original["snapshots"][0]["raw_path"])
    path.write_text("corrupted", encoding="utf-8")
    with pytest.raises(AppError) as err:
        imported(store, platform="another")
    assert err.value.code == "raw_integrity_error"
    assert store.list_content()["total"] == 1


def test_external_evidence_cannot_be_returned_even_if_database_was_modified(store):
    owner = confirmed(store, imported(store, "我喜欢茶", visibility="public"), fact="我喜欢茶")
    external = imported(store, "对方喜欢咖啡", authorship="external", visibility="public")
    with store.db.sessions.begin() as session:
        version = session.scalar(select(MemoryVersion).where(MemoryVersion.memory_id == owner["id"]))
        session.add(MemoryEvidence(version_id=version.id, block_id=external["blocks"][0]["id"]))
    assert store.retrieve("茶", "private") == []
    assert store.retrieve("茶", "public") == []
    with pytest.raises(AppError) as err:
        store.confirm_memory(owner["id"])
    assert err.value.code == "invalid_provenance"


def test_delete_reports_filesystem_cleanup_failure(store, monkeypatch):
    content = imported(store)
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if str(path) == content["snapshots"][0]["raw_path"]:
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    result = store.delete_content(content["id"])
    assert result == {"id": content["id"], "deleted": True, "raw_cleanup_pending": True}
    assert store.list_content()["total"] == 0


def test_repeated_extraction_preserves_user_corrections_and_approval(store):
    source = imported(store, "我曾经喜欢物理。\n\n现在我喜欢数学。")
    facts = [{"text": "我喜欢物理", "kind": "preference",
              "evidence_block_ids": [b["id"] for b in source["blocks"]]}]
    first = store.propose_facts(source["id"], facts)[0]
    assert store.propose_facts(source["id"], facts)[0]["id"] == first["id"]
    # Duplicate/reordered evidence and surrounding whitespace do not create new identities.
    equivalent = [{**facts[0], "text": "  我喜欢物理  ", "kind": " preference ",
                   "evidence_block_ids": list(reversed(facts[0]["evidence_block_ids"])) * 2}]
    assert store.propose_facts(source["id"], equivalent)[0]["id"] == first["id"]
    store.confirm_memory(first["id"])
    edited = store.correct_memory(first["id"], "我现在喜欢数学", visibility="public")
    assert store.propose_facts(source["id"], facts)[0] == edited
    approved = store.confirm_memory(first["id"])
    assert store.propose_facts(source["id"], facts)[0] == approved
    inactive = store.deactivate_memory(first["id"])
    assert store.propose_facts(source["id"], facts)[0] == inactive
    assert store.list_memories()["total"] == 1
    with store.db.sessions() as session:
        assert len(list(session.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "memory_proposed")))) == 1


def test_concurrent_proposals_share_one_identity(store):
    source = imported(store)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: proposed(store, source), range(8)))
    assert len({r["id"] for r in results}) == 1
    assert store.list_memories()["total"] == 1
    assert len(results[0]["versions"]) == 1


def test_upgrade_legacy_0001_preserves_corrected_facts_and_legacy_duplicates(store):
    source = imported(store)
    original = proposed(store, source)
    edited = store.correct_memory(original["id"], "用户更正后的偏好")
    store.confirm_memory(original["id"])
    duplicate = proposed(store, source, fact="a legacy duplicate")
    with store.db.engine.begin() as connection:
        connection.execute(text("UPDATE memory_versions SET text=:text WHERE memory_id=:id"),
                           {"text": original["text"], "id": duplicate["id"]})
        # Reconstruct the actual pre-0002 schema, retaining every existing row.
        connection.execute(text("DROP INDEX uq_memory_proposal_key"))
        connection.execute(text("ALTER TABLE memories DROP COLUMN proposal_key"))
        connection.execute(text("UPDATE alembic_version SET version_num='0001_initial'"))
    assert store.db.readiness()["ready"] is False
    store.db.initialize()
    assert store.db.readiness()["schema_revision"] == SCHEMA_HEAD
    retried = proposed(store, source)
    assert retried["id"] == original["id"]
    assert retried["text"] == edited["text"] and retried["status"] == "confirmed"
    assert len(retried["versions"]) == 2
    assert store.list_memories()["total"] == 2  # upgrade never deletes legacy rows
    store.db.initialize()
    assert proposed(store, source)["id"] == original["id"]


def test_storage_mutations_have_transactional_audit_without_content_text(store, monkeypatch):
    source = imported(store, "private source phrase")
    memory = proposed(store, source, fact="private fact phrase")
    store.confirm_memory(memory["id"])
    store.correct_memory(memory["id"], "private corrected phrase")
    store.deactivate_memory(memory["id"])
    store.update_content_visibility(source["id"], "public")
    store.delete_memory(memory["id"])
    store.delete_content(source["id"])
    with store.db.sessions() as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert [event.event_type for event in events] == [
            "content_imported", "memory_proposed", "memory_confirmed", "memory_corrected",
            "memory_deactivated", "content_visibility_updated", "memory_deleted", "content_deleted",
        ]
        details = json.dumps([event.detail for event in events])
        assert "private source phrase" not in details
        assert "private fact phrase" not in details
        assert "private corrected phrase" not in details
        assert all(event.detail["owner_id"] == store.settings.owner_id for event in events)
    source2 = imported(store, "another source")
    original_audit = store._audit

    def fail_after_audit(*args, **kwargs):
        original_audit(*args, **kwargs)
        raise AppError("test_failure", "Force rollback after audit insertion.")

    monkeypatch.setattr(store, "_audit", fail_after_audit)
    with pytest.raises(AppError):
        store.update_content_visibility(source2["id"], "public")
    assert store.get_content(source2["id"])["visibility"] == "private"
    with store.db.sessions() as session:
        assert len(list(session.scalars(select(AuditEvent).where(
            AuditEvent.resource_id == source2["id"])))) == 1
