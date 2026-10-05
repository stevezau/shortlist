"""Browser-only assistant setup and exact-plan review; never assistant tools."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from shortlist.server.assistant_auth import GrantPreset, capabilities_for_preset
from shortlist.server.assistant_auth.routes import BrowserOwner, require_browser_owner
from shortlist.server.catalogs.responses import RowTemplateDefinitionOut, SettingDefinitionOut
from shortlist.server.catalogs.settings import get_settings_catalog
from shortlist.server.catalogs.templates import get_template_catalog

from .changes import ChangeError
from .operation_models import AssistantChange

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_browser_owner)])
catalog_router = APIRouter(prefix="/catalogs", tags=["catalogs"], dependencies=[Depends(require_browser_owner)])


@catalog_router.get("/settings", response_model=list[SettingDefinitionOut])
def settings_catalog() -> list[dict]:
    return [item.model_dump(mode="json") for item in get_settings_catalog()]


@catalog_router.get("/templates", response_model=list[RowTemplateDefinitionOut])
def template_catalog() -> list[dict]:
    return [item.model_dump(mode="json") for item in get_template_catalog()]


@router.get("/status")
def status(request: Request) -> dict:
    runtime = getattr(request.app.state, "assistant_auth", None)
    return {
        "enabled": runtime is not None,
        "resource": runtime.oauth.resource if runtime else None,
        "issuer": runtime.oauth.issuer if runtime else None,
        "configuration_error": getattr(request.app.state, "assistant_configuration_error", None),
        "configuration_hint": (
            "Set SHORTLIST_MCP_URL to the canonical Shortlist URL followed by /mcp and restart. "
            "Use HTTPS outside loopback."
        ),
        "presets": {
            preset.value: sorted(cap.value for cap in capabilities_for_preset(preset)) for preset in GrantPreset
        },
        "setting_groups": sorted({setting.group.value for setting in get_settings_catalog()}),
    }


@router.get("/changes/{change_id}")
def review_change(
    change_id: str, request: Request, owner: Annotated[BrowserOwner, Depends(require_browser_owner)]
) -> dict:
    with request.app.state.sessions() as session:
        change = session.get(AssistantChange, change_id)
        if change is None or change.owner_account_id != owner.account_id:
            raise HTTPException(status_code=404, detail="change not found")
        from shortlist.server.db.models import iso_utc

        return {
            "change_id": change.id,
            "grant_id": change.grant_id,
            "client_id": change.client_id,
            "kind": change.kind,
            "summary": change.summary,
            "requirements": change.requirements,
            "effects": change.effects,
            "content_hash": change.content_hash,
            "expires_at": iso_utc(change.expires_at),
            "approved": change.approved_by is not None,
            "operation_id": change.operation_id,
        }


@router.post("/changes/{change_id}/approve")
def approve_change(
    change_id: str, request: Request, owner: Annotated[BrowserOwner, Depends(require_browser_owner)]
) -> dict:
    service = getattr(request.app.state, "assistant_changes", None)
    if service is None:
        raise HTTPException(status_code=503, detail="assistant access is disabled")
    try:
        return service.approve(change_id, owner_account_id=owner.account_id)
    except ChangeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
