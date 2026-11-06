from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from findyou.api import create_app
from findyou.config import Settings
from findyou.contracts import ImportRequest
from findyou.db import utcnow
from findyou.errors import AppError
from findyou.ops import Job
from findyou.runtime import Runtime


@pytest.fixture
def rt(tmp_path):
    cfg = Settings(_env_file=None, data_dir=tmp_path / "data", sqlite_journal_mode="DELETE",
                   api_token="test-token", llm_provider="fake")
    runtime = Runtime(cfg)
    runtime.db.initialize()
    yield runtime
    runtime.db.engine.dispose()


@pytest.fixture
def client(rt):
    return TestClient(create_app(runtime=rt), headers={"Authorization": "Bearer test-token"})


def confirmed(rt, public=True):
    c = rt.storage.import_content(ImportRequest(title="Source", text="我喜欢人工智能，正在开发 FindYou 项目。",
                                               visibility="public" if public else "private"))
    extraction = rt.worker.extract(c["id"])
    for memory in extraction["memories"]:
        rt.storage.confirm_memory(memory["id"])
    return c, extraction["memories"]


def test_authenticated_api_and_safe_errors(rt):
    client = TestClient(create_app(runtime=rt))
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/content").status_code == 401
    client.headers["Authorization"] = "Bearer test-token"
    response = client.post("/api/v1/conversations", json={"peer_id": "", "thread_key": "private-sentinel"})
    assert response.status_code == 422
    assert "private-sentinel" not in response.text


def test_import_worker_extract_confirm_and_draft(rt, client):
    job = client.post("/api/v1/imports", json={"title": "Me", "text": "我喜欢人工智能。", "visibility": "public"})
    assert job.status_code == 202
    assert rt.worker.run_once()
    imported = client.get("/api/v1/jobs/" + job.json()["id"]).json()
    assert imported["status"] == "succeeded"
    content_id = imported["result"]["id"]
    queued = client.post(f"/api/v1/content/{content_id}/extract").json()
    rt.worker.run_once()
    candidates = rt.queue.get(queued["id"])["result"]["memories"]
    assert all(m["status"] == "candidate" for m in candidates)
    assert rt.storage.retrieve("人工智能", "public") == []
    for memory in candidates:
        assert client.post(f"/api/v1/memories/{memory['id']}/confirm").status_code == 200
    conv = client.post("/api/v1/conversations", json={"peer_id": "alice", "thread_key": "video-1"}).json()
    response = client.post("/api/v1/chat/simulate", json={"conversation_id": conv["id"], "question": "人工智能"})
    assert response.status_code == 200, response.text
    assert response.json()["action"]["simulated"] is True
    assert response.json()["action"]["policy"]["platform_write"] is False
    assert response.json()["evidence"]
    history = client.get("/api/v1/conversations/" + conv["id"]).json()
    assert [m["direction"] for m in history["messages"]] == ["incoming", "draft"]
    assert history["summary"]["message_count"] == 2


def test_private_facts_never_reach_public_prompt(rt):
    confirmed(rt, public=False)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="public", scope="public")
    before = rt.services.page(__import__("findyou.ops", fromlist=["LLMCall"]).LLMCall, 20, 0)["total"]
    output = rt.services.simulate(conv["id"], "人工智能")
    assert not output["evidence"]
    assert "足够" in output["action"]["draft"]
    assert rt.services.page(__import__("findyou.ops", fromlist=["LLMCall"]).LLMCall, 20, 0)["total"] == before


def test_conversation_and_scope_isolation(rt):
    confirmed(rt)
    alice = rt.services.create_conversation(peer_id="alice", thread_key="thread-a")
    bob = rt.services.create_conversation(peer_id="bob", thread_key="thread-a")
    alice_other = rt.services.create_conversation(peer_id="alice", thread_key="thread-b")
    rt.services.simulate(alice["id"], "人工智能")
    assert len(rt.services.conversation(alice["id"])["messages"]) == 2
    assert rt.services.conversation(bob["id"])["messages"] == []
    assert rt.services.conversation(alice_other["id"])["messages"] == []


def test_duplicate_event_no_second_draft(rt):
    confirmed(rt)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    a = rt.services.simulate(conv["id"], "人工智能", "external-1")
    b = rt.services.simulate(conv["id"], "人工智能", "external-1")
    assert a["action"]["id"] == b["action"]["id"]
    assert b["duplicate"] is True
    assert len(rt.services.conversation(conv["id"])["messages"]) == 2


def test_revoked_source_not_reintroduced_by_old_answers(rt):
    source, _ = confirmed(rt)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    rt.services.simulate(conv["id"], "人工智能")
    rt.storage.update_content_visibility(source["id"], "private")
    result = rt.services.simulate(conv["id"], "人工智能")
    assert not result["evidence"]
    assert "人工智能" not in result["action"]["draft"]


def test_model_hallucinates_evidence_id_rejected(rt):
    from findyou.contracts import ModelResult
    confirmed(rt)
    rt.gateway.generate = lambda *args, **kwargs: ModelResult(data={"answer": "unsupported", "evidence_ids": ["bogus"]}, provider="fake", model="test")
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    with pytest.raises(AppError, match="eligible evidence"):
        rt.services.simulate(conv["id"], "人工智能")


def test_source_revocation_during_generation_fails_closed(rt):
    source, _ = confirmed(rt)
    original = rt.gateway.generate
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        rt.storage.update_content_visibility(source["id"], "private")
        return result
    rt.gateway.generate = revoke
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    with pytest.raises(AppError, match="Evidence changed"):
        rt.services.simulate(conv["id"], "人工智能")


def test_pause_and_unsupported_writes(client, rt):
    client.post("/api/v1/agent/stop")
    queued = client.post("/api/v1/imports", json={"text": "do not process yet"}).json()
    assert rt.worker.run_once() is False
    assert rt.queue.get(queued["id"])["status"] == "queued"
    client.post("/api/v1/agent/resume")
    assert rt.worker.run_once()
    assert client.post("/api/v1/actions/anything/approve").status_code == 409
    assert client.post("/api/v1/connectors/bilibili/run_bili", json={"params": {}}).status_code == 422


def test_jobs_lease_recovery_and_fencing(rt):
    job = rt.queue.enqueue("import", ImportRequest(text="testing").model_dump(), max_attempts=2)
    old = rt.queue.claim()
    assert old and old["id"] == job["id"]
    assert rt.queue.claim() is None
    with rt.db.sessions.begin() as session:
        session.get(Job, job["id"]).lease_until = utcnow() - timedelta(seconds=1)
    new = rt.queue.claim()
    assert new["attempts"] == 2
    rt.queue.finish(old, result={"wrong": True})
    assert rt.queue.get(job["id"])["status"] == "running"
    rt.queue.finish(new, result={"ok": True})
    assert rt.queue.get(job["id"])["result"] == {"ok": True}


def test_final_expired_job_not_stuck_and_retry_bound(rt):
    job = rt.queue.enqueue("import", {}, max_attempts=1)
    rt.queue.claim()
    with rt.db.sessions.begin() as session:
        session.get(Job, job["id"]).lease_until = utcnow() - timedelta(seconds=1)
    assert rt.queue.claim() is None
    assert rt.queue.get(job["id"])["status"] == "failed"


def test_missing_model_is_explicit_not_fake(rt):
    from findyou.models_gateway import Gateway
    cfg = rt.settings.model_copy(update={"llm_provider": "deepseek", "llm_api_key": None})
    with pytest.raises(AppError) as exc:
        Gateway(cfg).generate("reply", "system", {"question": "test", "evidence": []})
    assert exc.value.code == "model_not_configured"


def test_failed_generation_can_retry_same_received_event(rt):
    confirmed(rt)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    generate = rt.gateway.generate
    def failed(*args):
        raise AppError("model_timeout", "Timeout", 504, retryable=True)
    rt.gateway.generate = failed
    with pytest.raises(AppError):
        rt.services.simulate(conv["id"], "人工智能", "retry-1")
    rt.gateway.generate = generate
    result = rt.services.simulate(conv["id"], "人工智能", "retry-1")
    assert result["action"]["status"] == "draft"
    assert len(rt.services.conversation(conv["id"])["messages"]) == 2


def test_platform_risk_stops_worker_without_retry(rt):
    def risk(*args):
        raise AppError("connector_risk_control", "Risk control", 429, retryable=False)
    rt.bili.execute = risk
    job = rt.queue.enqueue("bilibili", {"operation": "search", "params": {"keyword": "test"}})
    assert rt.worker.run_once()
    assert rt.queue.get(job["id"])["status"] == "failed"
    assert rt.services.paused()
    assert not rt.worker.run_once()


def test_event_id_conflicting_text_rejected_even_after_draft(rt):
    confirmed(rt)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    rt.services.simulate(conv["id"], "人工智能", "immutable-event")
    with pytest.raises(AppError) as error:
        rt.services.simulate(conv["id"], "different text", "immutable-event")
    assert error.value.code == "event_conflict"


def test_concurrent_retries_return_one_persisted_draft(rt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    confirmed(rt)
    conv = rt.services.create_conversation(peer_id="alice", thread_key="video-1")
    generate = rt.gateway.generate
    def fail(*args):
        raise AppError("model_timeout", "Timeout", 504, retryable=True)
    rt.gateway.generate = fail
    with pytest.raises(AppError):
        rt.services.simulate(conv["id"], "人工智能", "retry-event")
    barrier = Barrier(2)
    def concurrent_generate(*args):
        result = generate(*args)
        barrier.wait(timeout=10)
        return result
    rt.gateway.generate = concurrent_generate
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rt.services.simulate(conv["id"], "人工智能", "retry-event"), range(2)))
    assert results[0]["action"]["id"] == results[1]["action"]["id"]
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert len(rt.services.conversation(conv["id"])["messages"]) == 2


def test_simulation_query_limit_matches_retrieval_contract(client):
    conv = client.post("/api/v1/conversations", json={"peer_id": "alice", "thread_key": "limit"}).json()
    accepted = client.post("/api/v1/chat/simulate", json={"conversation_id": conv["id"], "question": "x" * 2000})
    assert accepted.status_code == 200
    rejected = client.post("/api/v1/chat/simulate", json={"conversation_id": conv["id"], "question": "x" * 2001})
    assert rejected.status_code == 422
