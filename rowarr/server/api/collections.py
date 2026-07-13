"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name, prompt). Owner-only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from rowarr.engine.models import slugify
from rowarr.server.auth import require_owner
from rowarr.server.db.models import Collection, CollectionAudience

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=[Depends(require_owner)])

# Slugs reserved by the engine: `probe` is the throwaway Privacy Check row; `shared` prefixes every
# shared collection's label. A user-defined collection may not claim either.
RESERVED_SLUGS = {"probe", "shared"}
BUILDS = {"per_person", "shared"}
AUDIENCES = {"everyone", "subset"}
MEDIA = {"movie", "show", "both"}


class PromptIn(BaseModel):
    tone: str = "balanced"
    guidance: str = ""
    template: str = ""


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = "per_person"
    audience: str = "everyone"
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    size: int = Field(default=15, ge=5, le=30)
    media: str = "both"
    sort_order: int = 0
    name_template: str = ""
    source: str = "all_users"
    min_watchers: int = Field(default=2, ge=1)
    prompt: PromptIn = Field(default_factory=PromptIn)


def _validate(body: CollectionIn) -> None:
    if body.build not in BUILDS:
        raise HTTPException(422, f"build must be one of {sorted(BUILDS)}")
    if body.audience not in AUDIENCES:
        raise HTTPException(422, f"audience must be one of {sorted(AUDIENCES)}")
    if body.media not in MEDIA:
        raise HTTPException(422, f"media must be one of {sorted(MEDIA)}")


def _serialize(session, collection: Collection) -> dict:
    audience_ids = [
        row.user_id
        for row in session.query(CollectionAudience).filter_by(collection_id=collection.id).all()
    ]
    return {
        "id": collection.id,
        "slug": collection.slug,
        "name": collection.name,
        "build": collection.build,
        "audience": collection.audience,
        "audience_user_ids": audience_ids,
        "enabled": collection.enabled,
        "size": collection.size,
        "media": collection.media,
        "sort_order": collection.sort_order,
        "name_template": collection.name_template,
        "source": collection.source,
        "min_watchers": collection.min_watchers,
        "prompt": collection.prompt or {},
    }


def _unique_slug(session, base: str) -> str:
    base = base if base not in RESERVED_SLUGS else f"{base}_row"
    slug, n = base, 2
    while session.query(Collection).filter_by(slug=slug).first() is not None:
        slug = f"{base}_{n}"
        n += 1
    return slug


def _set_audience(session, collection: Collection, body: CollectionIn) -> None:
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    if body.audience == "subset":
        for user_id in dict.fromkeys(body.audience_user_ids):  # dedupe, keep order
            session.add(CollectionAudience(collection_id=collection.id, user_id=user_id))


@router.get("")
async def list_collections(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        collections = session.query(Collection).order_by(Collection.sort_order, Collection.id).all()
        return [_serialize(session, c) for c in collections]


@router.post("", status_code=201)
async def create_collection(body: CollectionIn, request: Request) -> dict:
    _validate(body)
    with request.app.state.sessions() as session:
        collection = Collection(
            slug=_unique_slug(session, slugify(body.name)),
            name=body.name,
            build=body.build,
            audience=body.audience,
            enabled=body.enabled,
            size=body.size,
            media=body.media,
            sort_order=body.sort_order,
            name_template=body.name_template,
            source=body.source,
            min_watchers=body.min_watchers,
            prompt=body.prompt.model_dump(),
        )
        session.add(collection)
        session.flush()
        _set_audience(session, collection, body)
        session.commit()
        return _serialize(session, collection)


@router.patch("/{collection_id}")
async def update_collection(collection_id: int, body: CollectionIn, request: Request) -> dict:
    _validate(body)
    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        collection.name = body.name
        collection.build = body.build
        collection.audience = body.audience
        collection.enabled = body.enabled
        collection.size = body.size
        collection.media = body.media
        collection.sort_order = body.sort_order
        collection.name_template = body.name_template
        collection.source = body.source
        collection.min_watchers = body.min_watchers
        collection.prompt = body.prompt.model_dump()
        _set_audience(session, collection, body)
        session.commit()
        return _serialize(session, collection)


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(collection_id: int, request: Request) -> None:
    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        if collection.slug == "picked":
            # The default per-person row is what makes an upgrade behaviour-neutral; disable it
            # instead of deleting so there's always a home for users with no other row.
            raise HTTPException(422, "the default 'picked' row can't be deleted — disable it instead")
        session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
        session.delete(collection)
        session.commit()
