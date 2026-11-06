"""Synthetic interview and biography workflows using only the offline model."""

import pytest
from fastapi.testclient import TestClient

from findyou.api import create_app
from findyou.config import Settings
from findyou.errors import AppError
from findyou.ops import Job
from findyou.runtime import Runtime


@pytest.fixture
def rt(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, sqlite_journal_mode="DELETE",
                        api_token="test", llm_provider="fake")
    runtime = Runtime(settings)
    runtime.db.initialize()
    yield runtime
    runtime.db.engine.dispose()


def add_fact(rt, topic, wording, *, cloud=False, confirmed=True):
    source = rt.life.save_source("合成材料", wording, cloud_allowed=cloud)
    candidate, = rt.life.propose(source["id"], [{"topic": topic, "kind": "fact", "text": wording,
        "evidence_block_ids": [source["blocks"][0]["id"]]}])
    if confirmed:
        rt.storage.confirm_memory(candidate["id"])
    return source, rt.life.get_fact(candidate["id"])


def answer(interviewer, view, text="我和同学办过一次纸飞机比赛。", *, cloud=False):
    return interviewer.answer(view["id"], view["question"]["id"], text, "request-1", cloud_allowed=cloud)


def test_local_interview_controls_known_topics_and_durable_idempotency(rt):
    add_fact(rt, "growth", "我从小在南方长大。")
    base = "/api/v1/life/interview"
    with TestClient(create_app(runtime=rt)) as client:
        assert client.get(base).status_code == 401
        client.headers.update({"Authorization": "Bearer test"})
        assert client.get(base).json()["status"] == "idle"
        started = client.post(f"{base}/start", json={}).json()
        assert started["question"]["topic"] == "events"
        path = f"{base}/{started['id']}"
        skipped = client.post(f"{path}/skip", json={"expected_revision": started["revision"]}).json()
        assert skipped["skipped_topics"] == ["events"]
        assert skipped["question"]["topic"] == "education"
        paused = client.post(f"{path}/pause", json={"expected_revision": skipped["revision"]}).json()
        assert paused["status"] == "paused" and paused["question"] is None
        reopened = client.post(f"{path}/reopen", json={"topic": "events", "expected_revision": paused["revision"]}).json()
        resumed = client.post(f"{path}/resume", json={"expected_revision": reopened["revision"]}).json()
        assert resumed["question"]["topic"] == "events"
        body = {"question_id": resumed["question"]["id"], "request_id": "example", "text": "一场纸飞机比赛。"}
        saved = client.post(f"{path}/answers", json=body).json()
        assert saved["question"]["topic"] == "education"
        assert saved["turns"][0]["job_id"] is None and saved["turns"][0]["answer_text"] == body["text"]
        assert client.post(f"{path}/answers", json={**body, "text": "另一个回答"}).status_code == 409
    reopened_rt = Runtime(rt.settings)
    try:
        duplicate = reopened_rt.story.interviewer.answer(started["id"], body["question_id"], body["text"], body["request_id"])
        assert len(duplicate["turns"]) == 1 and duplicate["turns"][0]["source_id"] == saved["turns"][0]["source_id"]
    finally:
        reopened_rt.db.engine.dispose()
    statuses = {item["topic"]: item for item in rt.story.coverage.get()["topics"]}
    assert statuses["growth"]["status"] == "covered" and statuses["growth"]["confirmed"] == 1
    assert statuses["events"]["status"] == "in_progress" and statuses["events"]["answers"] == 1


def test_failed_cloud_turn_retries_answer_and_revocation_redacts_question(rt, monkeypatch):
    interview = rt.story.interviewer
    started = interview.start()
    saved = answer(interview, started, cloud=True)
    turn = saved["turns"][0]
    assert saved["question"] is None and turn["status"] == "pending"
    original = rt.gateway.generate

    def fail(*args):
        raise AppError("fixture_failure", "Offline fixture failed", 502)

    monkeypatch.setattr(rt.gateway, "generate", fail)
    assert rt.worker.run_once()
    assert interview.get()["turns"][0]["job_status"] == "failed"
    assert interview.get()["question"]["topic"] == "growth"

    def follow(operation, system, payload):
        result = original(operation, system, payload)
        result.data.update(topic="growth", question="纸飞机比赛是一个具体的开头。小时候你喜欢在哪里做这些东西？")
        return result

    monkeypatch.setattr(rt.gateway, "generate", follow)
    retry = interview.retry(started["id"], turn["id"])
    assert retry["turns"][0]["source_id"] == turn["source_id"]
    assert retry["turns"][0]["job_id"] != turn["job_id"] and rt.worker.run_once()
    followup = interview.get()["question"]
    assert followup["topic"] == "growth" and "纸飞机" in followup["text"]
    assert len(rt.life.facts(status="candidate")) == 1
    source = rt.life.get_source(turn["source_id"])
    rt.life.authorize(source["id"], False, source["revision"])
    safe = interview.get()["question"]
    assert safe["topic"] == "growth" and "纸飞机" not in safe["text"] and safe["id"] != followup["id"]
    with pytest.raises(AppError) as exc:
        interview.answer(started["id"], followup["id"], "我在院子里做的。", "request-2")
    assert exc.value.code == "question_changed"


@pytest.mark.parametrize("lose_lease", [False, True])
def test_turn_generation_is_fenced_by_source_and_lease(rt, monkeypatch, lose_lease):
    interview = rt.story.interviewer
    started = interview.start()
    turn = answer(interview, started, cloud=True)["turns"][0]
    original = rt.gateway.generate

    def invalidate(operation, system, payload):
        result = original(operation, system, payload)
        if lose_lease:
            with rt.db.sessions.begin() as session:
                session.get(Job, turn["job_id"]).lease_token = "new-worker-token"
        else:
            source = rt.life.get_source(turn["source_id"])
            rt.life.authorize(source["id"], False, source["revision"])
        return result

    monkeypatch.setattr(rt.gateway, "generate", invalidate)
    assert rt.worker.run_once()
    assert not rt.life.facts(status="candidate")
    retained = interview.get()["turns"][0]
    assert retained["answer_text"] and retained["status"] != "processed"


def test_biography_manual_versions_retrieval_and_stale_redaction(rt):
    source, fact = add_fact(rt, "career", "我写过一份合成项目报告。", cloud=True)
    _, candidate = add_fact(rt, "career", "未确认的材料。", confirmed=False)
    biography = rt.story.biography
    with pytest.raises(AppError):
        biography.generate("career", [fact["id"], candidate["id"]], 0)
    first = biography.generate("career", [fact["id"]], 0)
    assert first["latest"]["markdown"] == fact["text"] and first["current_version"] == 1
    second = biography.edit("career", "我写过一份合成项目报告，也整理了笔记。", 1)
    assert second["latest"]["origin"] == "manual" and second["current_version"] == 2
    assert biography.relevant("项目报告", [fact])[0]["markdown"] == second["latest"]["markdown"]
    with TestClient(create_app(runtime=rt), headers={"Authorization": "Bearer test"}) as client:
        exported = client.get("/api/v1/life/biography/export")
        assert exported.headers["content-type"].startswith("text/markdown")
        assert second["latest"]["markdown"] in exported.text
    rt.life.authorize(source["id"], False, source["revision"])
    assert all(v["stale"] and v["markdown"] is None for v in biography.get("career")["versions"])
    assert "项目报告" not in biography.export() and not biography.relevant("项目报告", [fact])
    with pytest.raises(AppError) as exc:
        biography.edit("career", "复制旧稿内容。", 2)
    assert exc.value.code == "chapter_stale"


def test_cloud_chapter_selected_facts_and_final_revision_fence(rt, monkeypatch):
    source, fact = add_fact(rt, "career", "我做过纸风车项目。", cloud=True)
    _, other = add_fact(rt, "events", "另一个未选中的项目。", cloud=True)
    biography = rt.story.biography
    queued = biography.generate("career", [fact["id"]], 0, use_cloud=True,
                                fact_versions={fact["id"]: fact["version_id"]})
    original = rt.gateway.generate

    def invalidate(operation, system, payload):
        assert [item["id"] for item in payload["facts"]] == [fact["id"]]
        result = original(operation, system, payload)
        rt.life.authorize(source["id"], False, source["revision"])
        return result

    monkeypatch.setattr(rt.gateway, "generate", invalidate)
    assert rt.worker.run_once()
    assert rt.queue.get(queued["job_id"])["status"] == "failed"
    assert biography.get("career")["current_version"] == 0
    monkeypatch.setattr(rt.gateway, "generate", original)
    queued = biography.generate("career", [other["id"]], 0, use_cloud=True,
                                fact_versions={other["id"]: other["version_id"]})
    assert rt.worker.run_once() and rt.queue.get(queued["job_id"])["status"] == "succeeded"
    latest = biography.get("career")["latest"]
    assert latest["origin"] == "cloud" and latest["markdown"] == other["text"]


@pytest.mark.parametrize("kind", ["turn", "chapter"])
def test_source_revoked_during_reservation_never_reaches_model(rt, monkeypatch, kind):
    if kind == "turn":
        interview = rt.story.interviewer
        turn = answer(interview, interview.start(), cloud=True)["turns"][0]
        source = rt.life.get_source(turn["source_id"])
        job_id = turn["job_id"]
    else:
        source, fact = add_fact(rt, "career", "我做过纸风车项目。", cloud=True)
        job_id = rt.story.biography.generate("career", [fact["id"]], 0, use_cloud=True,
                                             fact_versions={fact["id"]: fact["version_id"]})["job_id"]

    original = rt.agent._reserve
    submissions = []

    def revoke(run_id, operation):
        original(run_id, operation)
        rt.life.authorize(source["id"], False, source["revision"])

    def gateway(operation, system, payload):
        submissions.append(operation)
        raise AssertionError("Revoked sources must never be submitted")

    monkeypatch.setattr(rt.agent, "_reserve", revoke)
    monkeypatch.setattr(rt.gateway, "generate", gateway)
    assert rt.worker.run_once() and not submissions
    job = rt.queue.get(job_id)
    assert job["status"] == "failed" and job["error"]["code"] == "life_source_changed"


def test_cloud_chapter_requires_exact_selected_versions(rt):
    _, fact = add_fact(rt, "career", "我做过纸风车项目。", cloud=True)
    current = rt.life.correct(fact["id"], "我参与过纸风车项目。", fact["current_version"])
    rt.storage.confirm_memory(fact["id"])
    path = "/api/v1/life/biography/chapters/career/generate"
    body = {"fact_ids": [fact["id"]], "expected_version": 0, "use_cloud": True}
    with TestClient(create_app(runtime=rt), headers={"Authorization": "Bearer test"}) as client:
        assert client.post(path, json=body).status_code == 422
        for versions in ({}, {fact["id"]: fact["version_id"]}):
            response = client.post(path, json={**body, "fact_versions": versions})
            assert response.status_code == 409 and response.json()["error"]["code"] == "life_fact_changed"
        assert rt.story.biography.get("career")["job_id"] is None
        queued = client.post(path, json={**body, "fact_versions": {fact["id"]: current["version_id"]}})
        assert queued.status_code == 200 and queued.json()["job_status"] == "queued"
