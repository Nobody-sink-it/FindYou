"""Authenticated REST interfaces for explicit capture and autonomous dry runs."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from findyou.capture import video_identifier
from findyou.db import utcnow
from findyou.errors import AppError


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureRequest(StrictRequest):
    video_id: str = Field(min_length=1, max_length=2048)
    page: int = Field(default=1, ge=1, le=100)
    sort: Literal["hot", "time"] = "hot"
    include_subtitle: bool = False


class ReplyRequest(StrictRequest):
    target_id: str = Field(min_length=1, max_length=100)
    role_version_id: str | None = Field(default=None, max_length=100)
    objective: str = Field(default="参与有意义的讨论，了解不同观点", min_length=1, max_length=1000)


class CycleRequest(StrictRequest):
    video_ids: list[str] = Field(default_factory=list, max_length=3)
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    max_videos: int = Field(default=1, ge=1, le=3)
    max_targets: int = Field(default=2, ge=1, le=3)
    comment_sort: Literal["time", "hot"] = "time"
    include_subtitle: bool = False
    role_version_id: str | None = Field(default=None, max_length=100)
    objective: str = Field(default="寻找可自然交流的话题，不追求每条都回复", min_length=1, max_length=1000)


class MissionRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    config: CycleRequest
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=10, le=10080)


class EnabledRequest(StrictRequest):
    enabled: bool


class RoleRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    card: str | dict


class CardRequest(StrictRequest):
    card: str | dict


class VersionRequest(StrictRequest):
    version_id: str = Field(min_length=1, max_length=100)


class TextRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=2000)


class DraftRequest(StrictRequest):
    body: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)


class FeedbackRequest(StrictRequest):
    action_id: str | None = Field(default=None, min_length=1, max_length=100)
    draft_id: str | None = Field(default=None, min_length=1, max_length=100)
    verdict: Literal["accepted", "edited", "rejected"]
    edited_text: str | None = Field(default=None, max_length=4000)
    reason: str = Field(default="", max_length=2000)


def build_social_router(rt):
    router = APIRouter(tags=["social-agent"])
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0)]

    def config(body):
        value = body.model_dump()
        value["video_ids"] = [video_identifier(v) for v in value["video_ids"]]
        value["role_version_id"] = rt.identity.select_role(value["role_version_id"])["id"]
        return value

    @router.post("/videos/capture", status_code=202)
    def capture(body: CaptureRequest):
        value = body.model_dump()
        value["video_id"] = video_identifier(value["video_id"])
        return rt.queue.enqueue("capture", value)

    @router.get("/videos")
    def videos(limit: Limit = 20, offset: Offset = 0, q: str | None = None,
               sort: Literal["observed_desc", "observed_asc"] = "observed_desc"):
        return rt.social.list_videos(limit=limit, offset=offset, q=q, sort=sort)

    @router.get("/videos/{identifier}")
    def video(identifier: str):
        return rt.social.get_video(identifier)

    @router.get("/threads")
    def threads(limit: Limit = 20, offset: Offset = 0, q: str | None = None,
                video_id: str | None = None, sort: Literal["observed_desc", "observed_asc"] = "observed_desc"):
        return rt.social.list_threads(limit=limit, offset=offset, q=q, video_id=video_id, sort=sort)

    @router.get("/threads/{identifier}")
    def thread(identifier: str):
        return rt.social.get_thread(identifier)

    @router.get("/threads/{identifier}/messages")
    def messages(identifier: str, limit: Limit = 50, offset: Offset = 0, parent_id: str | None = None):
        return rt.social.list_messages(identifier, limit=limit, offset=offset, parent_id=parent_id)

    @router.get("/platform-messages/{identifier}")
    def message(identifier: str):
        return rt.social.get_message(identifier)

    @router.get("/console/summary")
    def summary():
        return rt.social.summary()

    @router.get("/contacts")
    def contacts(limit: Limit = 20, offset: Offset = 0, q: str | None = None):
        return rt.identity.list_contacts(limit, offset, q)

    @router.get("/contacts/{identifier}")
    def contact(identifier: str):
        return rt.identity.get_contact(identifier)

    @router.get("/contacts/{identifier}/memories")
    def contact_memories(identifier: str, limit: Limit = 20, offset: Offset = 0, status: str | None = None):
        return rt.identity.list_contact_memories(identifier, limit, offset, status)

    @router.post("/contacts/{identifier}/extract-memories", status_code=202)
    def contact_extract(identifier: str):
        rt.identity.get_contact(identifier)
        return rt.queue.enqueue("contact_extract", {"contact_id": identifier}, max_attempts=1)

    @router.get("/contacts/{identifier}/relationship-assessments")
    def relationship(identifier: str):
        rt.identity.get_contact(identifier)
        return {"items": [{"contact_id": identifier, "basis": "public_observations_only",
            "interaction_willingness": "insufficient_evidence", "familiarity": "unassessed",
            "interest_in_owner": "unknown", "mutually_confirmed_relationship": None,
            "confidence": "low", "reason": "尚未执行平台发送，公开观测不能证明双方关系。",
            "evaluated_at": utcnow().isoformat() + "Z"}], "assessment_mode": "conservative_baseline"}

    @router.patch("/contact-memories/{identifier}")
    def correct(identifier: str, body: TextRequest):
        return rt.identity.correct_memory(identifier, body.text)

    @router.post("/contact-memories/{identifier}/{operation}")
    def memory_operation(identifier: str, operation: Literal["confirm", "deactivate"]):
        fn = rt.identity.confirm_memory if operation == "confirm" else rt.identity.deactivate_memory
        return fn(identifier)

    @router.delete("/contact-memories/{identifier}")
    def memory_delete(identifier: str):
        return rt.identity.delete_memory(identifier)

    @router.get("/roles")
    def roles():
        rt.identity.select_role()
        return rt.identity.list_roles()

    @router.post("/roles", status_code=201)
    def create_role(body: RoleRequest):
        return rt.identity.create_role(body.name, body.card)

    @router.get("/roles/{identifier}/versions")
    def versions(identifier: str):
        return rt.identity.role_versions(identifier)

    @router.post("/roles/{identifier}/versions", status_code=201)
    def add_version(identifier: str, body: CardRequest):
        return rt.identity.add_role_version(identifier, body.card)

    @router.post("/roles/{identifier}/approve")
    def approve(identifier: str, body: VersionRequest):
        # role_versions is authorized even for pending versions; bind approval to the specified role.
        if body.version_id not in {v["id"] for v in rt.identity.role_versions(identifier)["items"]}:
            raise AppError("not_found", "Role version not found", 404)
        return rt.identity.approve_role_version(body.version_id)

    @router.post("/roles/default")
    def default(body: VersionRequest):
        return rt.identity.set_default_role(body.version_id)

    @router.post("/agent/replies", status_code=202)
    def reply(body: ReplyRequest):
        value = body.model_dump()
        rt.social.get_message(value["target_id"])
        value["role_version_id"] = rt.identity.select_role(value["role_version_id"])["id"]
        return rt.queue.enqueue("agent_reply", value, max_attempts=1)

    @router.post("/agent/cycles", status_code=202)
    def cycle(body: CycleRequest):
        return rt.queue.enqueue("agent_cycle", {"config": config(body)}, max_attempts=1)

    @router.get("/agent/missions")
    def missions(limit: Limit = 20, offset: Offset = 0):
        return rt.agent.missions(limit, offset)

    @router.post("/agent/missions", status_code=201)
    def mission(body: MissionRequest):
        return rt.agent.create_mission(body.name, config(body.config), body.enabled, body.interval_minutes)

    @router.patch("/agent/missions/{identifier}")
    def toggle(identifier: str, body: EnabledRequest):
        return rt.agent.set_mission(identifier, body.enabled)

    @router.get("/runs")
    def runs(limit: Limit = 20, offset: Offset = 0):
        return rt.agent.runs(limit, offset)

    @router.get("/runs/{identifier}")
    def run(identifier: str):
        return rt.agent.get_run(identifier)

    @router.get("/runs/{identifier}/steps")
    def steps(identifier: str):
        return {"items": rt.agent.get_run(identifier)["stages"]}

    @router.post("/runs/{identifier}/retry", status_code=202)
    def retry(identifier: str):
        return rt.agent.retry(identifier)

    @router.get("/agent/drafts")
    def drafts(limit: Limit = 20, offset: Offset = 0):
        return rt.agent.drafts(limit, offset)

    @router.patch("/agent/drafts/{identifier}")
    def edit_draft(identifier: str, body: DraftRequest):
        return rt.agent.edit_draft(identifier, body.body, body.expected_version)

    @router.post("/feedback", status_code=201)
    def feedback(body: FeedbackRequest):
        values = body.model_dump(exclude={"action_id", "draft_id"})
        if bool(body.action_id) == bool(body.draft_id):
            raise AppError("invalid_feedback", "Supply exactly one action_id or draft_id", 422)
        if body.draft_id:
            return rt.agent.record_feedback(body.draft_id, **values)
        return rt.identity.record_feedback(body.action_id, **values)

    @router.get("/agent/expression-preferences")
    def preferences():
        return rt.agent.preferences()

    @router.get("/usage")
    def usage():
        return rt.agent.usage()

    @router.post("/agent/expression-preferences/{identifier}/{operation}")
    def preference(identifier: str, operation: Literal["confirm", "deactivate"]):
        return rt.agent.set_preference(identifier, "confirmed" if operation == "confirm" else "inactive")

    return router
