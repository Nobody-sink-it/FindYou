"""End-to-end synthetic platform + offline model; never posts or uses real credentials."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from findyou.agent_models import AgentDraft, AgentMission, AgentRun, AgentUsageReservation
from findyou.api import create_app
from findyou.config import Settings
from findyou.content_models import Memory
from findyou.contracts import ImportRequest, ModelResult
from findyou.db import utcnow
from findyou.errors import AppError
from findyou.runtime import Runtime

BV = "BV0000000001"


class BiliFixture:
    def __init__(self):
        self.operations = []

    def status(self):
        return {"configured": True, "read_only": True, "fixture": True}

    def execute(self, operation, params):
        assert operation in {"video_info", "comments", "subtitle", "search", "recommend"}
        self.operations.append(operation)
        if operation == "video_info":
            data = {"bvid": BV, "title": "倾听与建议", "desc": "讨论如何倾听。",
                    "owner": {"mid": "88", "name": "Fixture creator"}, "stat": {"reply": 30}}
        elif operation == "comments":
            data = {"items": [{"rpid": "100", "root": None, "parent": None,
                "content": "我希望被认真理解，而不是直接得到建议。", "user": {"mid": "1", "name": "Fixture A"},
                "time": "2026-11-04T08:00:00Z", "replies_preview": [
                    {"rpid": "101", "root": "100", "parent": "100", "content": "先听完确实重要。",
                     "user": {"mid": "2", "name": "Fixture B"}},
                    {"rpid": "102", "root": "100", "parent": "101", "content": "我也这样想。",
                     "user": {"mid": "1", "name": "Fixture A"}}]}], "total": 30, "page": 1}
        elif operation == "subtitle":
            raise AppError("connector_auth_required", "Fixture requires login", 401)
        else:
            data = {"items": [{"bvid": BV}]}
        return {"data": data, "observed_at": "2026-11-04T10:00:00Z", "read_only": True}


@pytest.fixture
def rt(tmp_path):
    cfg = Settings(_env_file=None, data_dir=tmp_path, sqlite_journal_mode="DELETE",
                   api_token="test", llm_provider="fake")
    rt = Runtime(cfg, bili=BiliFixture())
    rt.db.initialize()
    yield rt
    rt.db.engine.dispose()


def capture(rt):
    result = rt.capture.capture(BV)
    thread = rt.social.get_thread(result["thread_ids"][0])
    return result, thread["root_message"]


def client(rt):
    return TestClient(create_app(runtime=rt), headers={"Authorization": "Bearer test"})


def test_capture_api_worker_draft_pinned_role_and_idempotency(rt):
    api = client(rt)
    job = api.post("/api/v1/videos/capture", json={"video_id": f"https://www.bilibili.com/video/{BV}?x=1"})
    assert job.status_code == 202
    assert rt.worker.run_once()
    result = rt.queue.get(job.json()["id"])
    assert result["status"] == "succeeded", result
    assert api.get("/api/v1/console/summary").json()["messages"] == 3
    thread = api.get("/api/v1/threads").json()["items"][0]
    messages = api.get(f"/api/v1/threads/{thread['id']}/messages").json()["items"]
    target = next(m for m in messages if m["external_id"] == "102")
    job = api.post("/api/v1/agent/replies", json={"target_id": target["id"]})
    pinned = job.json()["payload"]["role_version_id"]
    assert rt.worker.run_once()
    reply = rt.queue.get(job.json()["id"])
    assert reply["status"] == "succeeded", reply
    run = api.get("/api/v1/runs/" + reply["result"]["run_id"]).json()
    assert [m["external_id"] for m in run["input_snapshot"]["path"]] == ["100", "101", "102"]
    assert run["role_version_id"] == pinned
    drafts = api.get("/api/v1/agent/drafts").json()["items"]
    assert len(drafts) == 1 and drafts[0]["target"]["parent"] == "102"
    assert drafts[0]["target"]["root"] == "100" and drafts[0]["status"] == "draft"
    assert run["usage"]["input_tokens"] is None and run["usage"]["simulated"]
    duplicate = rt.agent.process(target["id"], pinned)
    assert duplicate["duplicate"] is True
    assert api.post("/api/v1/actions/anything/publish").json()["error"]["code"] == "platform_write_disabled"


def test_cycle_discovers_reads_and_only_drafts(rt):
    api = client(rt)
    job = api.post("/api/v1/agent/cycles", json={"keyword": "倾听", "max_targets": 1})
    assert rt.worker.run_once()
    result = rt.queue.get(job.json()["id"])
    assert result["status"] == "succeeded", result
    assert result["result"]["decisions"]
    assert rt.bili.operations == ["search", "video_info", "comments"]
    assert len(rt.agent.drafts()["items"]) == 1


def test_contact_extraction_and_feedback_are_not_owner_facts(rt):
    _, target = capture(rt)
    contact = rt.identity.observe_message(target)
    extraction = rt.agent.extract_contact(contact["id"])
    assert extraction["memories"]
    with rt.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Memory)) == 0
    generated = rt.agent.process(target["id"])
    draft_id = generated["output"]["draft_id"]
    feedback = rt.agent.record_feedback(draft_id, "rejected", reason="少用泛化安慰，多回应具体原文。")
    preference = feedback["preference_candidate_id"]
    role = rt.identity.select_role()["id"]
    assert not rt.agent.context.build(target["id"], role)["approved_expression_preferences"]
    rt.agent.set_preference(preference, "confirmed")
    assert rt.agent.context.build(target["id"], role)["approved_expression_preferences"][0]["id"] == preference
    rt.agent.set_preference(preference, "inactive")
    assert not rt.agent.context.build(target["id"], role)["approved_expression_preferences"]


def test_public_context_does_not_include_private_owner_facts(rt):
    _, target = capture(rt)
    content = rt.storage.import_content(ImportRequest(text="我有一个私密倾听习惯 PRIVATE-SENTINEL", visibility="private"))
    facts = rt.worker.extract(content["id"])["memories"]
    for fact in facts:
        rt.storage.confirm_memory(fact["id"])
    package = rt.agent.context.build(target["id"])
    assert not package["owner_evidence"]
    assert "PRIVATE-SENTINEL" not in str(package)


def test_revocation_during_generation_rejects_draft_and_redacts_run(rt):
    captured, target = capture(rt)
    original = rt.gateway.generate
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        rt.storage.update_content_visibility(captured["content_ids"][1], "private")
        return result
    rt.gateway.generate = revoke
    with pytest.raises(AppError):
        rt.agent.process(target["id"])
    assert rt.agent.drafts()["total"] == 0
    run = rt.agent.runs()["items"][0]
    assert rt.agent.get_run(run["id"])["input_snapshot"]["redacted"]


def test_revoked_sources_hide_prior_drafts(rt):
    captured, target = capture(rt)
    rt.agent.process(target["id"])
    rt.storage.update_content_visibility(captured["content_ids"][1], "private")
    draft = rt.agent.drafts()["items"][0]
    assert draft["status"] == "stale" and not draft["body"]


def test_no_model_call_when_full_role_exceeds_budget(rt):
    _, target = capture(rt)
    rt.settings.agent_context_tokens = 100
    with pytest.raises(AppError) as error:
        rt.agent.process(target["id"])
    assert error.value.code == "context_budget_exceeded"
    assert rt.agent.runs()["total"] == 0


def test_daily_call_reservation_includes_failed_calls(rt):
    _, target = capture(rt)
    rt.settings.agent_daily_model_calls = 1
    def fail(*args):
        raise AppError("model_timeout", "Fixture timeout", 504, retryable=True)
    rt.gateway.generate = fail
    with pytest.raises(AppError):
        rt.agent.process(target["id"])
    role = rt.identity.create_role("Different", "公开身份的AI；不编造事实。")
    rt.identity.approve_role_version(role["current_version_id"])
    with pytest.raises(AppError) as error:
        rt.agent.process(target["id"], role["current_version_id"])
    assert error.value.code == "model_budget_exceeded"


def test_draft_edit_conflict_and_role_changes_leave_history(rt):
    _, target = capture(rt)
    run = rt.agent.process(target["id"])
    draft_id = run["output"]["draft_id"]
    old_version = rt.agent.drafts()["items"][0]["role_version_id"]
    role = rt.identity.create_role("New expression", "温和且具体，说明AI身份。")
    rt.identity.approve_role_version(role["current_version_id"])
    rt.identity.set_default_role(role["current_version_id"])
    assert rt.agent.drafts()["items"][0]["role_version_id"] == old_version
    result = rt.agent.edit_draft(draft_id, "我是AI助手。修订草稿。", 1)
    assert result["version"] == 2
    with pytest.raises(AppError) as error:
        rt.agent.edit_draft(draft_id, "stale edit", 1)
    assert error.value.code == "draft_conflict"


def test_paused_missions_do_not_schedule_and_enabled_mission_is_singleton(rt):
    role = rt.identity.select_role()["id"]
    config = {"video_ids": [BV], "max_videos": 1, "max_targets": 1,
              "objective": "倾听", "role_version_id": role}
    mission = rt.agent.create_mission("Fixture mission", config, enabled=True)
    rt.services.set_paused(True)
    assert rt.worker.run_once() is False
    assert not rt.bili.operations
    rt.services.set_paused(False)
    rt.agent.schedule_due()
    rt.agent.schedule_due()
    assert rt.worker.run_once()
    assert len(rt.agent.runs()["items"]) == 2
    rt.agent.set_mission(mission["id"], False)
    with rt.db.sessions() as session:
        assert not session.get(AgentMission, mission["id"]).enabled


def test_wrong_evidence_and_missing_disclosure_rejected(rt):
    _, target = capture(rt)
    rt.gateway.generate = lambda *args: ModelResult(provider="fake", model="fixture", data={
        "decision": "draft", "reason": "fixture", "answer": "pretend human", "evidence_ids": ["bad-id"]})
    with pytest.raises(AppError) as error:
        rt.agent.process(target["id"])
    assert error.value.code == "ungrounded_output"
    assert rt.agent.drafts()["items"] == []


def test_concurrent_duplicate_does_not_double_draft(rt):
    _, target = capture(rt)
    rt.identity.select_role()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rt.agent.process(target["id"]), range(2)))
    assert len({r["run_id"] for r in results}) == 1
    with rt.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentDraft)) == 1


def test_unexpected_exception_fails_run_and_can_explicitly_retry(rt):
    _, target = capture(rt)
    original = rt.gateway.generate
    def crash(*args):
        raise RuntimeError("private exception sentinel")
    rt.gateway.generate = crash
    with pytest.raises(AppError) as error:
        rt.agent.process(target["id"])
    assert "sentinel" not in str(error.value)
    run = rt.agent.runs()["items"][0]
    assert run["status"] == "failed"
    rt.gateway.generate = original
    job = rt.agent.retry(run["id"])
    assert rt.worker.run_once()
    assert rt.queue.get(job["id"])["status"] == "succeeded"


def test_abandoned_generation_is_recoverable(rt):
    _, target = capture(rt)
    run = rt.agent.process(target["id"])
    with rt.db.sessions.begin() as session:
        row = session.get(AgentRun, run["run_id"])
        row.status = "running"
        row.created_at = utcnow() - timedelta(hours=1)
    rt.agent.recover_abandoned()
    assert rt.agent.get_run(run["run_id"])["error"]["code"] == "run_abandoned"


def test_disabled_mission_queued_job_does_not_read_or_generate(rt):
    config = {"video_ids": [BV], "max_videos": 1, "max_targets": 1,
              "objective": "倾听", "role_version_id": rt.identity.select_role()["id"]}
    mission = rt.agent.create_mission("Fixture", config, enabled=True)
    rt.agent.schedule_due()
    rt.agent.set_mission(mission["id"], False)
    assert rt.worker.run_once()
    assert rt.bili.operations == [] and rt.agent.runs()["total"] == 0


def test_contact_extract_job_only_persists_ids(rt):
    _, target = capture(rt)
    contact = rt.identity.observe_message(target)
    job = rt.queue.enqueue("contact_extract", {"contact_id": contact["id"]})
    assert rt.worker.run_once()
    result = rt.queue.get(job["id"])["result"]
    assert "memories" not in result and result["memory_ids"]
    assert "希望" not in str(result)


def test_cycle_target_cap_includes_failed_generation(rt):
    old = rt.bili.execute
    def more(operation, params):
        result = old(operation, params)
        if operation == "comments":
            result["data"]["items"].extend([
                {"rpid": "200", "root": None, "parent": None, "content": "Different root",
                 "user": {"mid": "3", "name": "Fixture C"}},
                {"rpid": "300", "root": None, "parent": None, "content": "Third root",
                 "user": {"mid": "4", "name": "Fixture D"}}])
        return result
    rt.bili.execute = more
    def fail(*args):
        raise AppError("model_output_invalid", "Fixture invalid output", 502)
    rt.gateway.generate = fail
    rt.agent.cycle({"video_ids": [BV], "max_targets": 1, "role_version_id": rt.identity.select_role()["id"],
                    "objective": "倾听"})
    with rt.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentUsageReservation)) == 1


def test_late_recovered_run_cannot_create_second_draft(rt):
    _, target = capture(rt)
    entered, release = Event(), Event()
    lock, count = Lock(), [0]
    original = rt.gateway.generate
    def blocked(*args):
        with lock:
            count[0] += 1
            first = count[0] == 1
        if first:
            entered.set()
            assert release.wait(15)
        return original(*args)
    rt.gateway.generate = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rt.agent.process, target["id"])
        try:
            assert entered.wait(10)
            run = rt.agent.runs()["items"][0]
            with rt.db.sessions.begin() as session:
                session.get(AgentRun, run["id"]).created_at = utcnow() - timedelta(hours=1)
            rt.agent.recover_abandoned()
            job = rt.agent.retry(run["id"])
            assert rt.worker.run_once()
            assert rt.queue.get(job["id"])["status"] == "succeeded"
        finally:
            release.set()
        with pytest.raises(AppError) as error:
            future.result()
        assert error.value.code == "run_execution_lost"
    assert rt.agent.drafts()["total"] == 1
    assert rt.agent.get_run(run["id"])["status"] == "failed"
