"""Authenticated endpoints for life sources, personal facts and scoped exports."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from findyou.life_story_api import build_life_story_router


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRequest(Request):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=200000)
    kind: Literal["article", "journal", "post", "project"] = "article"
    cloud_allowed: bool = False


class CopyRequest(Request):
    content_id: str
    cloud_allowed: bool = False


class Authorization(Request):
    cloud_allowed: bool
    expected_revision: int = Field(ge=1)


class ProfileRequest(Request):
    fields: dict[str, Annotated[str, Field(max_length=1500)]] = Field(max_length=6)


class FactEdit(Request):
    text: str = Field(min_length=1, max_length=10000)
    expected_version: int = Field(ge=1)
    details: dict | None = None


class FactVersion(Request):
    expected_version: int = Field(ge=1)


class ShareRequest(Request):
    memory_id: str
    version_id: str
    channel: Literal["bilibili", "readme"]
    text: str = Field(min_length=1, max_length=2000)


def build_life_router(rt):
    router = APIRouter(prefix="/life", tags=["life"])
    life = rt.life

    @router.get("/sources")
    def sources():
        return life.sources()

    @router.post("/sources", status_code=201)
    def source_create(body: SourceRequest):
        return life.save_source(**body.model_dump())

    @router.post("/sources/copy", status_code=201)
    def source_copy(body: CopyRequest):
        return life.copy_source(**body.model_dump())

    @router.get("/sources/{identifier}")
    def source(identifier: str):
        return life.get_source(identifier)

    @router.patch("/sources/{identifier}/authorization")
    def authorization(identifier: str, body: Authorization):
        return life.authorize(identifier, **body.model_dump())

    @router.delete("/sources/{identifier}")
    def source_delete(identifier: str):
        life.get_source(identifier, detail=False)
        return rt.storage.delete_content(identifier)

    @router.post("/sources/{identifier}/extract", status_code=202)
    def extract(identifier: str):
        return life.enqueue_extract(identifier)

    @router.get("/profile")
    def profile():
        return {"items": life.profile()}

    @router.post("/profile", status_code=201)
    def profile_create(body: ProfileRequest):
        return {"items": life.save_profile(body.fields)}

    @router.get("/facts")
    def facts(topic: str | None = None, status: Literal["candidate", "confirmed", "inactive"] | None = None,
              q: Annotated[str | None, Query(max_length=1000)] = None,
              limit: Annotated[int, Query(ge=1, le=100)] = 50,
              offset: Annotated[int, Query(ge=0)] = 0):
        records = life.facts(topic, status, limit=None)
        if q:
            matched = {m["id"] for m in life.retrieve(q, "private", "local", limit=None)}
            records = [m for m in records if m["id"] in matched or
                       (m["text"] and q.casefold() in m["text"].casefold())]
        return {"items": records[offset:offset + limit], "total": len(records), "limit": limit, "offset": offset}

    @router.get("/facts/{identifier}")
    def fact(identifier: str):
        return life.get_fact(identifier)

    @router.patch("/facts/{identifier}")
    def edit_fact(identifier: str, body: FactEdit):
        return life.correct(identifier, body.text, body.expected_version, body.details)

    @router.post("/facts/{identifier}/confirm")
    def confirm_fact(identifier: str, body: FactVersion):
        life.get_fact(identifier)
        rt.storage.confirm_memory(identifier, expected_version=body.expected_version)
        return life.get_fact(identifier)

    @router.post("/facts/{identifier}/deactivate")
    def deactivate_fact(identifier: str):
        life.get_fact(identifier)
        rt.storage.deactivate_memory(identifier)
        return life.get_fact(identifier)

    @router.delete("/facts/{identifier}")
    def fact_delete(identifier: str):
        life.get_fact(identifier)
        return rt.storage.delete_memory(identifier)

    @router.get("/timeline")
    def timeline():
        records = [f for f in life.facts(limit=None) if f["kind"] == "event" or f["details"].get("start")]
        records.sort(key=lambda f: (f["details"].get("start") or "9999", f["id"]))
        return {"items": records}

    @router.get("/shares")
    def shares():
        return {"items": life.shares()}

    @router.post("/shares", status_code=201)
    def share(body: ShareRequest):
        return life.share(body.memory_id, body.version_id, body.channel, body.text)

    @router.delete("/shares/{identifier}")
    def revoke(identifier: str):
        life.revoke_share(identifier)
        return {"deleted": True}

    @router.get("/readme")
    def readme():
        items = [s for s in life.shares("readme") if s["active"]]
        markdown = "# 关于我\n\n" + "\n\n".join(s["text"] for s in items) + "\n"
        return {"items": items, "markdown": markdown}

    @router.get("/readme/export")
    def export_readme():
        return Response(readme()["markdown"], media_type="text/markdown",
                        headers={"Content-Disposition": 'attachment; filename="README.md"'})

    @router.get("/retrieve")
    def retrieve(q: Annotated[str, Query(min_length=1, max_length=1000)],
                 depth: Literal["standard", "deep"] = "standard"):
        facts = life.retrieve(q, "private", "local", 8)
        chapters = rt.story.biography.relevant(q, facts) if depth == "deep" else []
        return {"profile": life.profile(), "facts": facts, "chapters": chapters}

    router.include_router(build_life_story_router(rt))
    return router
