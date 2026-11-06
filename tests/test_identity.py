import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from findyou.config import Settings
from findyou.content_models import Memory, Owner
from findyou.db import Base, Database
from findyou.errors import AppError
from findyou.identity import IdentityService
from findyou.identity_models import ContactMemory, ExpressionFeedback, ExpressionRoleVersion
from findyou.ops import Action, Conversation, Message


class SocialStub:
    def __init__(self):
        self.messages = {}

    def get_message(self, id):
        if id not in self.messages:
            raise AppError("message_not_found", "Message unavailable", 404)
        return copy.deepcopy(self.messages[id])


@pytest.fixture
def identity(tmp_path):
    settings = Settings(data_dir=tmp_path, sqlite_journal_mode="DELETE", llm_provider="fake", _env_file=None)
    db = Database(settings)
    db.initialize()
    Base.metadata.create_all(db.engine)
    service = IdentityService(settings, db, SocialStub())
    yield service
    db.engine.dispose()


def message(service, uid="u1", id="m1", **updates):
    payload = {"id": id, "author_uid": uid, "author_name": "同名用户", "platform": "bilibili",
               "video_id": "video1", "thread_id": "thread1", "source_id": "source1", "body": "我喜欢茶。"}
    payload.update(updates)
    service.social.messages[id] = payload
    return service.observe_message(payload)


def propose(service, contact, id="m1", **updates):
    fact = {"text": "喜欢茶", "category": "preference", "epistemic": "self_report", "evidence_ids": [id]}
    fact.update(updates)
    return service.propose_memories(contact["id"], [fact])[0]


def other_owner(service):
    settings = service.settings.model_copy(update={"owner_id": "other"})
    with service.db.sessions.begin() as session:
        if not session.get(Owner, "other"):
            session.add(Owner(id="other", name="Other"))
    return IdentityService(settings, service.db, service.social)


def test_contact_identity_not_name_or_video_and_owner_isolation(identity):
    first = message(identity)
    again = message(identity, id="m2", video_id="different", author_name="新名字")
    assert again["id"] == first["id"]
    assert again["author_name"] == "新名字"
    assert again["relationship"] == "unknown" and "interaction_count" not in again
    second = message(identity, uid="u2", id="m3")
    assert second["id"] != first["id"]
    assert identity.list_contacts(q="新名字")["total"] == 1
    assert identity.list_contacts(q="%")["total"] == 0
    other = other_owner(identity)
    assert other.list_contacts()["total"] == 0
    with pytest.raises(AppError) as error:
        other.get_contact(first["id"])
    assert error.value.status == 404
    assert message(other)["id"] != first["id"]
    for uid in (None, "", " ", 123):
        with pytest.raises(AppError):
            identity.observe_message({"author_uid": uid, "author_name": "同名用户"})
    assert identity.list_contacts()["total"] == 2


def test_evidence_authorship_and_contact_retrieval_are_strict(identity):
    first = message(identity)
    second = message(identity, uid="u2", id="m2")
    with pytest.raises(AppError, match="this contact"):
        propose(identity, first, id="m2")
    first_memory = propose(identity, first)
    second_memory = propose(identity, second, id="m2", text="另一个对象的秘密")
    assert first_memory["epistemic_label"] == "对方自述"
    assert first_memory["status"] == "active"
    assert [m["id"] for m in identity.retrieve(first["id"])] == [first_memory["id"]]
    assert [m["id"] for m in identity.retrieve(second["id"])] == [second_memory["id"]]
    fallback = identity.retrieve(first["id"], query="秘密")
    assert [m["id"] for m in fallback] == [first_memory["id"]]
    assert fallback[0]["retrieval"]["mode"] == "recent_fallback"
    infer = propose(identity, first, text="可能爱好传统文化", epistemic="inference")
    assert infer["status"] == "candidate"
    assert len(identity.retrieve(first["id"])) == 1
    identity.confirm_memory(infer["id"])
    assert len(identity.retrieve(first["id"])) == 2
    other = other_owner(identity)
    for call in (lambda: other.list_contact_memories(first["id"]), lambda: other.retrieve(first["id"]),
                 lambda: other.confirm_memory(first_memory["id"]),
                 lambda: other.correct_memory(first_memory["id"], "hack"),
                 lambda: other.delete_memory(first_memory["id"])):
        with pytest.raises(AppError) as error:
            call()
        assert error.value.status == 404
    with identity.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Memory)) == 0


@pytest.mark.parametrize("change", ["deleted", "edited", "private", "wrong_author", "wrong_platform", "wrong_owner"])
def test_changed_or_revoked_sources_fail_closed_and_redact_history(identity, change):
    contact = message(identity)
    memory = propose(identity, contact, text="来源隐私摘录")
    identity.correct_memory(memory["id"], "修正版来源隐私")
    identity.confirm_memory(memory["id"])
    if change == "deleted":
        del identity.social.messages["m1"]
    else:
        key, value = {"edited": ("body", "完全改变了"), "private": ("visibility", "private"),
                      "wrong_author": ("author_uid", "u2"),
                      "wrong_platform": ("platform", "elsewhere"),
                      "wrong_owner": ("owner_id", "other")}[change]
        identity.social.messages["m1"][key] = value
    assert identity.retrieve(contact["id"]) == []
    listed = identity.list_contact_memories(contact["id"])["items"][0]
    assert listed["stale"] and listed["text"] is None
    assert all(v["text"] is None and v["stale"] for v in listed["versions"])
    assert "来源隐私" not in json.dumps(listed, ensure_ascii=False)
    with pytest.raises(AppError) as error:
        identity.confirm_memory(memory["id"])
    assert error.value.code == "stale_contact_evidence"
    with identity.db.sessions() as session:
        stored = session.get(ContactMemory, memory["id"])
        assert len(stored.versions) == 2
        assert "body" not in stored.versions[0]["evidence"][0]


def test_every_evidence_item_must_be_current_and_limit_after_eligibility(identity):
    contact = message(identity)
    message(identity, id="m2")
    valid = propose(identity, contact)
    propose(identity, contact, text="多个证据", evidence_ids=["m1", "m2"])
    del identity.social.messages["m2"]
    assert [m["id"] for m in identity.retrieve(contact["id"], limit=1)] == [valid["id"]]
    with pytest.raises(AppError):
        propose(identity, contact, evidence_ids=["m1", "missing"])


def test_correction_reproposal_and_delete_preserve_versions(identity):
    contact = message(identity)
    first = propose(identity, contact)
    edited = identity.correct_memory(first["id"], "现在喜欢咖啡")
    assert edited["status"] == "candidate" and len(edited["versions"]) == 2
    assert identity.retrieve(contact["id"]) == []
    assert propose(identity, contact) == edited
    confirmed = identity.confirm_memory(first["id"])
    assert confirmed["versions"][1]["confirmed_at"]
    assert propose(identity, contact) == confirmed
    retrieved = identity.retrieve(contact["id"], "咖啡")[0]
    assert retrieved["id"] == first["id"]
    assert "versions" not in retrieved and "喜欢茶" not in json.dumps(retrieved, ensure_ascii=False)
    inactive = identity.deactivate_memory(first["id"])
    assert propose(identity, contact) == inactive
    assert identity.retrieve(contact["id"]) == []
    deleted = identity.delete_memory(first["id"])
    assert deleted["status"] == "deleted" and deleted["text"] is None
    assert len(deleted["versions"]) == 2
    assert propose(identity, contact) == deleted
    assert identity.list_contact_memories(contact["id"])["total"] == 0
    assert identity.list_contact_memories(contact["id"], status="deleted")["total"] == 1


def test_parallel_proposals_and_builtin_are_idempotent(identity):
    contact = message(identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: propose(identity, contact), range(8)))
    assert len({row["id"] for row in results}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        roles = list(pool.map(lambda _: identity.select_role(), range(8)))
    assert len({row["id"] for row in roles}) == 1
    assert roles[0]["approval_origin"] == "builtin"
    assert "AI" in roles[0]["card"]["identity"]


def test_role_cards_are_fixed_and_new_versions_need_approval(identity):
    builtin = identity.select_role()
    card = {"voice": "简洁"}
    role = identity.create_role("简洁", card)
    card["voice"] = "caller mutation"
    first = identity.role_versions(role["id"])["items"][0]
    assert first["card"] == {"voice": "简洁"}
    with pytest.raises(AppError) as error:
        identity.get_role_version(first["id"])
    assert error.value.code == "role_not_approved"
    with pytest.raises(AppError):
        identity.set_default_role(first["id"])
    approved = identity.approve_role_version(first["id"])
    assert approved["approval_origin"] == "owner"
    identity.set_default_role(first["id"])
    second = identity.add_role_version(role["id"], {"voice": "详细"})
    assert second["version"] == 2 and second["sha256"] != first["sha256"]
    assert identity.select_role()["id"] == first["id"]
    assert identity.get_role_version(first["id"]) == approved
    with pytest.raises(AppError):
        identity.select_role(second["id"])
    identity.approve_role_version(second["id"])
    identity.set_default_role(second["id"])
    assert identity.select_role()["id"] == second["id"]
    assert identity.select_role(first["id"])["sha256"] == first["sha256"]
    assert identity.select_role(builtin["id"])["card"] == builtin["card"]
    assert identity.list_roles()["items"][1]["current_version"] == 2
    other = other_owner(identity)
    assert other.list_roles()["items"] == []
    for call in (lambda: other.get_role_version(first["id"]),
                 lambda: other.approve_role_version(second["id"]),
                 lambda: other.role_versions(role["id"]), lambda: other.set_default_role(first["id"])):
        with pytest.raises(AppError) as error:
            call()
        assert error.value.status == 404
    assert other.select_role()["id"] != builtin["id"]


def test_card_tampering_is_rejected_instead_of_reusing_approval(identity):
    role = identity.select_role()
    with identity.db.sessions.begin() as session:
        session.get(ExpressionRoleVersion, role["id"]).card = {"forged": "card"}
    with pytest.raises(AppError) as error:
        identity.select_role()
    assert error.value.code == "role_integrity_error"


def test_feedback_is_scoped_and_never_creates_owner_facts(identity):
    with identity.db.sessions.begin() as session:
        conv = Conversation(owner_id=identity.settings.owner_id, peer_id="u1", thread_key="t")
        session.add(conv)
        session.flush()
        msg = Message(conversation_id=conv.id, direction="incoming", text="hello")
        session.add(msg)
        session.flush()
        action = Action(owner_id=identity.settings.owner_id, conversation_id=conv.id,
                        incoming_message_id=msg.id, draft="原来的草稿")
        session.add(action)
        session.flush()
        action_id = action.id
    feedback = identity.record_feedback(action_id, "edited", "更自然的表达", "这个场景用短句")
    assert feedback["learning_status"] == "recorded_only"
    assert feedback["edited_text"] == "更自然的表达"
    identity.record_feedback(action_id, "accepted")
    identity.record_feedback(action_id, "rejected", reason="不适合")
    with identity.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Memory)) == 0
        assert session.scalar(select(func.count()).select_from(ExpressionFeedback)) == 3
        assert session.get(Action, action_id).draft == "原来的草稿"
    other = other_owner(identity)
    with pytest.raises(AppError) as error:
        other.record_feedback(action_id, "accepted")
    assert error.value.status == 404
    for call in (lambda: identity.record_feedback(action_id, "bad"),
                 lambda: identity.record_feedback(action_id, "edited"),
                 lambda: identity.record_feedback(action_id, "accepted", "new text")):
        with pytest.raises(AppError):
            call()


def test_service_input_boundaries(identity):
    contact = message(identity)
    for call in (lambda: identity.list_contacts(limit=True), lambda: identity.list_contacts(offset=-1),
                 lambda: identity.list_contacts(q=42), lambda: identity.create_role("x", {"bad": float("nan")}),
                 lambda: identity.propose_memories(contact["id"], [{"text": "fact", "evidence_ids": ["m1"]}]),
                 lambda: identity.list_contact_memories(contact["id"], status="madeup"),
                 lambda: identity.retrieve(contact["id"], query=None)):
        with pytest.raises(AppError):
            call()


def test_retrieval_ranks_cross_comment_chinese_topics_before_newer_unrelated_records(identity):
    contact = message(identity)
    related = propose(identity, contact, text="周末喜欢登山和徒步，常在山里走很久。")
    propose(identity, contact, text="最近学做意大利面。")
    propose(identity, contact, text="常听爵士乐。")
    results = identity.retrieve(contact["id"], "你平时会去登山吗？周末一般怎么安排？", limit=1)
    assert [item["id"] for item in results] == [related["id"]]
    assert results[0]["retrieval"]["mode"] == "lexical_overlap"
    assert 0 < results[0]["retrieval"]["lexical_similarity"] <= 1
    assert results[0]["retrieval"]["semantic_similarity_evaluated"] is False
    assert "versions" not in results[0]
    assert identity.retrieve(contact["id"], "登山", limit=1)[0]["id"] == related["id"]
    assert identity.retrieve(contact["id"], "山", limit=1)[0]["id"] == related["id"]
    english = propose(identity, contact, text="I enjoy hiking outdoors.")
    assert identity.retrieve(contact["id"], "What do you enjoy about HIKING?", limit=1)[0]["id"] == english["id"]


def test_recent_fallback_remains_small_current_and_contact_scoped(identity):
    contact = message(identity)
    old = propose(identity, contact, text="喜欢茶")
    corrected = propose(identity, contact, text="以前经常骑车")
    identity.correct_memory(corrected["id"], "现在改乘地铁")
    inference = propose(identity, contact, text="可能擅长摄影", epistemic="inference")
    inactive = propose(identity, contact, text="曾经学过鼓")
    identity.deactivate_memory(inactive["id"])
    propose(identity, contact, text="爱好排球")
    propose(identity, contact, text="会做木工")
    # Give the stale record its own message so other eligible evidence stays valid.
    message(identity, id="m2")
    stale = propose(identity, contact, text="爱好游泳", id="m2")
    del identity.social.messages["m2"]
    other = message(identity, uid="u2", id="m3")
    other_memory = propose(identity, other, id="m3", text="其他人的近况")
    fallback = identity.retrieve(contact["id"], "量子引力", limit=8)
    assert len(fallback) == 2
    assert all(item["contact_id"] == contact["id"] for item in fallback)
    excluded = {corrected["id"], inference["id"], inactive["id"], stale["id"], other_memory["id"]}
    assert not excluded & {item["id"] for item in fallback}
    assert all(item["retrieval"] == {"mode": "recent_fallback", "lexical_similarity": 0.0,
                                    "semantic_similarity_evaluated": False} for item in fallback)
    assert len(identity.retrieve(contact["id"], "量子引力", limit=1)) == 1
    assert identity.retrieve(contact["id"], "茶", limit=1)[0]["id"] == old["id"]
    identity.confirm_memory(corrected["id"])
    current = identity.retrieve(contact["id"], "你现在还乘地铁吗", limit=1)[0]
    assert current["id"] == corrected["id"] and current["text"] == "现在改乘地铁"
    assert "骑车" not in json.dumps(current, ensure_ascii=False)
    assert identity.retrieve(contact["id"], "", limit=1)[0]["retrieval"]["mode"] == "recent"
