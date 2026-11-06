"""Owner-authenticated interview and biography management routes."""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field


class StoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRequest(StoryRequest):
    topic: str | None = Field(default=None, min_length=1, max_length=32)


class AnswerRequest(StoryRequest):
    question_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=1, max_length=100)
    cloud_allowed: bool = False


class TransitionRequest(StartRequest):
    expected_revision: int = Field(ge=1)


class GenerateRequest(StoryRequest):
    fact_ids: list[str] = Field(min_length=1, max_length=20)
    fact_versions: dict[str, str] | None = Field(default=None, max_length=20)
    expected_version: int = Field(ge=0)
    use_cloud: bool = False


class EditRequest(StoryRequest):
    markdown: str = Field(min_length=1, max_length=30000)
    expected_version: int = Field(ge=0)


def build_life_story_router(rt) -> APIRouter:
    router = APIRouter(tags=["life-story"])

    @router.get("/interview")
    def interview():
        return rt.story.interviewer.get()

    @router.post("/interview/start")
    def start(body: StartRequest | None = None):
        return rt.story.interviewer.start(body.topic if body else None)

    @router.post("/interview/{interview_id}/answers")
    def answer(interview_id: str, body: AnswerRequest):
        return rt.story.interviewer.answer(interview_id, body.question_id, body.text, body.request_id,
                                           cloud_allowed=body.cloud_allowed)

    @router.post("/interview/{interview_id}/turns/{turn_id}/retry")
    def retry(interview_id: str, turn_id: str):
        return rt.story.interviewer.retry(interview_id, turn_id)

    @router.post("/interview/{interview_id}/{action}")
    def transition(interview_id: str, action: Literal["pause", "resume", "skip", "reopen"], body: TransitionRequest):
        return rt.story.interviewer.transition(interview_id, action, body.expected_revision, body.topic)

    @router.get("/coverage")
    def coverage():
        return rt.story.coverage.get()

    @router.get("/biography/chapters")
    def chapters():
        return rt.story.biography.list_chapters()

    @router.get("/biography/chapters/{topic}")
    def chapter(topic: str):
        return rt.story.biography.get(topic)

    @router.post("/biography/chapters/{topic}/generate")
    def generate(topic: str, body: GenerateRequest):
        return rt.story.biography.generate(topic, **body.model_dump())

    @router.patch("/biography/chapters/{topic}")
    def edit(topic: str, body: EditRequest):
        return rt.story.biography.edit(topic, body.markdown, body.expected_version)

    @router.get("/biography/export")
    def export():
        return Response(rt.story.biography.export(), media_type="text/markdown",
                        headers={"Content-Disposition": 'attachment; filename="biography.md"',
                                 "Cache-Control": "no-store"})

    return router
