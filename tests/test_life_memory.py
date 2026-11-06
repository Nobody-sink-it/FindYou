"""Consent and provenance regressions across the new and existing entry points."""

import pytest
from fastapi.testclient import TestClient

from findyou.api import create_app
from findyou.config import Settings
from findyou.runtime import Runtime


@pytest.fixture
def setup(tmp_path):
    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path, sqlite_journal_mode="DELETE",
                          llm_provider="fake", api_token="local-test"))
    rt.db.initialize()
    with TestClient(create_app(runtime=rt), headers={"Authorization": "Bearer local-test"}) as client:
        yield rt, client
    rt.db.engine.dispose()


def test_life_source_requires_consent_and_preserves_provenance(setup):
    rt, client = setup
    url = "/api/v1/life"
    body = {"title": "合成经历", "text": "我在 2022 年搬家。私人背景只在原文。", "kind": "journal"}
    source = client.post(url + "/sources", json=body).json()
    sid = source["id"]
    assert client.post(url + f"/sources/{sid}/extract").status_code == 409
    assert client.post(f"/api/v1/content/{sid}/extract").status_code == 409
    assert client.patch(f"/api/v1/content/{sid}/visibility", json={"visibility": "public"}).status_code == 409
    assert client.patch(url + f"/sources/{sid}/authorization",
                        json={"cloud_allowed": True, "expected_revision": 1}).status_code == 200
    assert client.post(url + "/sources", json=body).json()["id"] == sid
    job = client.post(url + f"/sources/{sid}/extract").json()
    assert rt.worker.run_once()
    result = client.get(f"/api/v1/jobs/{job['id']}").json()["result"]
    assert set(result) == {"source_id", "memory_ids"}
    mid = result["memory_ids"][0]
    fact = client.get(url + f"/facts/{mid}").json()
    assert fact["status"] == "candidate" and fact["evidence"][0]["content_id"] == sid
    assert not rt.storage.retrieve("搬家", purpose="model")
    assert client.post(url + f"/facts/{mid}/confirm", json={"expected_version": 1}).status_code == 200
    assert rt.storage.retrieve("搬家", purpose="model")[0]["id"] == mid
    assert not rt.storage.retrieve("搬家", "public", purpose="bilibili")
    share = client.post(url + "/shares", json={"memory_id": mid, "version_id": fact["version_id"],
                          "channel": "bilibili", "text": "我曾经搬家。"}).json()
    assert share["active"]
    public = rt.storage.retrieve("搬家", "public", purpose="bilibili")
    assert public[0]["text"] == "我曾经搬家。" and public[0]["evidence"] == []
    assert not client.get(url + "/readme").json()["items"]
    conv = rt.services.create_conversation(peer_id="reader", thread_key="test", platform="bilibili")
    answer = rt.services.simulate(conv["id"], "搬家", external_id="one")
    assert "我曾经搬家" in answer["action"]["draft"]
    client.patch(url + f"/sources/{sid}/authorization", json={"cloud_allowed": False, "expected_revision": 2})
    assert not rt.storage.retrieve("搬家", "public", purpose="bilibili")
    assert client.get(url + f"/facts/{mid}").json()["stale"]
    assert client.get(f"/api/v1/memories/{mid}").json()["text"] is None
    assert rt.services.simulate(conv["id"], "搬家", external_id="one")["action"]["draft"] is None
    assert all(m["text"] is None for m in rt.services.conversation(conv["id"])["messages"]
               if m["direction"] == "draft")


def test_queued_and_in_flight_revocation_are_fenced(setup):
    rt, client = setup
    source = rt.life.save_source("合成资料", "一段用于权限回归的文字。", cloud_allowed=True)
    sid = source["id"]
    queued = rt.life.enqueue_extract(sid)
    rt.life.authorize(sid, False, 1)
    assert rt.worker.run_once()
    assert rt.queue.get(queued["id"])["error"]["code"] == "life_source_changed"
    rt.life.authorize(sid, True, 2)
    original = rt.gateway.generate

    def revoke_during_call(*args):
        result = original(*args)
        rt.life.authorize(sid, False, 3)
        return result

    rt.gateway.generate = revoke_during_call
    queued = rt.life.enqueue_extract(sid)
    assert rt.worker.run_once()
    assert rt.queue.get(queued["id"])["status"] == "failed"
    assert not client.get("/api/v1/life/facts").json()["items"]


def test_temporal_conflicts_and_manual_profile(setup):
    rt, client = setup
    source = rt.life.save_source("合成时间轴", "以前倾向独处，后来更喜欢团队合作。")
    block_id = source["blocks"][0]["id"]

    def statement(body, year):
        return rt.life.propose(source["id"], [{"text": body, "kind": "viewpoint", "topic": "values",
                               "start": year, "key": "团队", "evidence_block_ids": [block_id]}])[0]

    old = statement("我倾向独处", "2018")
    rt.storage.confirm_memory(old["id"])
    new = statement("我喜欢团队合作", "2024")
    assert not new["details"]["conflict"]
    rt.storage.confirm_memory(new["id"])
    conflict = statement("我不想参加团队", "2024")
    assert conflict["details"]["conflict"]
    assert client.post(f"/api/v1/life/facts/{conflict['id']}/confirm",
                       json={"expected_version": 1}).status_code == 409
    profile = client.post("/api/v1/life/profile", json={"fields": {"interests": "木工"}}).json()["items"][0]
    rt.storage.confirm_memory(profile["id"])
    assert rt.life.profile()[0]["text"] == "兴趣：木工"
    assert not rt.storage.retrieve("木工", purpose="model")
