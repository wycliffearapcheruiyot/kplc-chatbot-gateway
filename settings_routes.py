"""
settings_routes.py

Admin endpoints behind the panel's "Environment" tab. Both need X-Admin-Token.

  GET /settings
      Every service in settings_catalog.CATALOG with, per variable:
        override     value stored in MongoDB by this panel (null = none)
        env          value the service reported from its real environment (null = not reported)
        has_override whether an override exists
      Values are returned in full, secrets included.

  PUT /settings/{service}    body: {"set": {"NAME": "value"}, "reset": ["NAME"]}
      Stores overrides (set) and removes them (reset -> the service falls back
      to its real env var). Only editable variables of that service are accepted.
      Returns the updated service. Services pick the change up within ~10 s.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import settings_catalog as catalog
from app_config import cfg
from runtime_config import SETTINGS_COLLECTION

LOCAL_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


class SettingsUpdate(BaseModel):
    set: dict[str, str] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


def _iso(dt):
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _service_view(service: dict, doc: dict) -> dict:
    values, env = doc.get("values") or {}, doc.get("env") or {}
    return {
        "id": service["id"],
        "label": service["label"],
        "description": service["description"],
        "env_reported_at": _iso(doc.get("env_reported_at")),
        "updated_at": _iso(doc.get("updated_at")),
        "vars": [
            {**v, "override": values.get(v["name"]), "has_override": v["name"] in values, "env": env.get(v["name"])}
            for v in service["vars"]
        ],
    }


def build_router(db, require_admin) -> APIRouter:
    router = APIRouter(tags=["settings"], dependencies=[Depends(require_admin)])

    def col():
        return db[SETTINGS_COLLECTION]

    @router.get("/settings")
    def get_settings():
        cfg.refresh(force=True)  # makes sure the gateway's own env report is current
        docs = {d["_id"]: d for d in col().find({})}
        return {"services": [_service_view(s, docs.get(s["id"], {})) for s in catalog.CATALOG]}

    @router.put("/settings/{service_id}")
    def update_settings(service_id: str, update: SettingsUpdate, request: Request):
        service = catalog.SERVICES.get(service_id)
        if service is None:
            raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")

        errors, to_set, to_reset = [], {}, []
        for name, value in update.set.items():
            spec = catalog.find_var(service_id, name)
            if spec is None:
                errors.append(f"{name}: not a variable of {service_id}.")
            elif not spec["editable"]:
                errors.append(f"{name}: set at deploy time; change it in the host's dashboard.")
            elif (problem := catalog.validate(spec, value)):
                errors.append(f"{name} {problem}")
            else:
                to_set[name] = value.strip()
        for name in update.reset:
            spec = catalog.find_var(service_id, name)
            if spec is None or not spec["editable"]:
                errors.append(f"{name}: cannot be reset.")
            else:
                to_reset.append(name)

        # Lock-out guard: a browser whose origin is not allowed cannot call the
        # gateway at all, so refuse a list that would exclude the admin panel itself.
        if service_id == "gateway" and "ALLOWED_ORIGINS" in to_set:
            origin = (request.headers.get("origin") or "").rstrip("/")
            allowed = catalog.normalize_origins(to_set["ALLOWED_ORIGINS"]) + list(LOCAL_ORIGINS)
            if origin and origin not in allowed:
                errors.append(
                    f"ALLOWED_ORIGINS must keep the origin you are using right now ({origin}), "
                    "otherwise this panel could no longer reach the gateway."
                )
        if errors:
            raise HTTPException(status_code=422, detail=" ".join(errors))
        to_reset = [n for n in to_reset if n not in to_set]  # a name can't be set and unset at once

        ops = {}
        now = datetime.now(timezone.utc)
        if to_set or to_reset:
            ops["$set"] = {**{f"values.{n}": v for n, v in to_set.items()}, "updated_at": now}
        if to_reset:
            ops["$unset"] = {f"values.{n}": "" for n in to_reset}
        if ops:
            col().update_one({"_id": service_id}, ops, upsert=True)
        if service_id == "gateway":
            cfg.invalidate()
        return _service_view(service, col().find_one({"_id": service_id}) or {})

    return router
