"""
session_manager.py

Orchestrates "Start demo" across the two backends this gateway fronts:

    1. dataset backend  -> is the model-weights dataset on Kaggle? (build it if not)
    2. session backend  -> push the Kaggle kernel, track starting/ready/ended

The session backend remains the source of truth for the session itself (the
Kaggle notebook calls ITS webhooks). This module only adds the dataset gate
in front and reshapes the answer for the frontends. The one thing it stores
in MongoDB (`session_state` collection, document {"_id": "session"}) is a
"pending_start" flag: "the user asked to start, we're waiting for the dataset".
Keeping that in Mongo instead of memory means a Render restart or a free-tier
sleep can't lose the request.

Status values the frontends see (`status` field):

    idle                 nothing running
    preparing_dataset    model files are being fetched into Kaggle (first run only)
    starting             Kaggle notebook is booting / loading the model
    ready                chat works
    error                something failed; `error` says what

Advancing the flow needs no background thread: while `pending_start` is set,
every GET /session/status re-checks the dataset and, the moment it's ready,
starts the session. The frontend is polling anyway.
"""

from datetime import datetime, timedelta, timezone

import backends
from app_config import cfg

SESSION_DOC_ID = "session"

# The session backend reports "ended"; frontends only need "idle" for that.
_STATUS_FOR_STATE = {
    "idle": "idle",
    "starting": "starting",
    "ready": "ready",
    "ended": "idle",
    "error": "error",
}


class SessionError(Exception):
    """A session action could not be completed (an upstream service failed)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _col(db):
    return db["session_state"]


def _doc(db) -> dict:
    return _col(db).find_one({"_id": SESSION_DOC_ID}) or {}


def _set(db, **fields) -> None:
    _col(db).update_one(
        {"_id": SESSION_DOC_ID}, {"$set": {**fields, "updated_at": _now()}}, upsert=True
    )


def _pending_max() -> timedelta:
    # The dataset backend gives up after 90 min by default; don't outlive it.
    return timedelta(minutes=cfg.get_int("DATASET_PENDING_MAX_MINUTES", 100))


def _claim_pending(db) -> bool:
    """Atomically clears pending_start. True for exactly one caller, so two
    browsers polling at once can't both start the session."""
    return (
        _col(db).find_one_and_update(
            {"_id": SESSION_DOC_ID, "pending_start": True},
            {"$set": {"pending_start": False, "updated_at": _now()}},
        )
        is not None
    )


# --- response shaping ---------------------------------------------------------

def _compose(sess: dict, **extra) -> dict:
    """Session-backend state -> the shape the frontends consume."""
    state = sess.get("state", "idle")
    return {
        "status": _STATUS_FOR_STATE.get(state, "idle"),
        "state": state,
        "started_at": sess.get("started_at"),
        "ready_at": sess.get("ready_at"),
        "ended_at": sess.get("ended_at"),
        "error": sess.get("error_detail"),
        **extra,
    }


def _blank(status: str, **extra) -> dict:
    return {
        "status": status,
        "state": status,
        "started_at": None,
        "ready_at": None,
        "ended_at": None,
        "error": None,
        **extra,
    }


def _dataset_view(ds: dict) -> dict:
    view = {"status": ds.get("status")}
    if ds.get("error"):
        view["error"] = ds["error"]
    if ds.get("kaggle_run"):
        view["kaggle_run"] = ds["kaggle_run"]
    return view


# --- public API -----------------------------------------------------------------

def start_session(db) -> dict:
    """POST /session/start. Idempotent: a live session or an in-progress
    dataset build is reported, never restarted."""
    try:
        return _start(db)
    except backends.UpstreamError as e:
        raise SessionError(str(e)) from e


def get_status(db) -> dict:
    """GET /session/status. Also advances a pending start (see module docstring)."""
    try:
        return _status(db)
    except backends.UpstreamError as e:
        raise SessionError(str(e)) from e


# --- internals -------------------------------------------------------------------

def _start(db) -> dict:
    current = backends.session_status()
    if current.get("state") in ("starting", "ready"):
        return _compose(current, triggered=False)

    if _doc(db).get("pending_start"):
        # Dataset build already in progress from an earlier click: act as a poll.
        return dict(_status(db), triggered=False)

    if not backends.dataset_gate_enabled():
        started = backends.session_start()
        return _compose(started, triggered=bool(started.get("triggered", True)))

    ds = backends.dataset_ensure()
    return _after_dataset(db, ds)


def _after_dataset(db, ds: dict) -> dict:
    status = ds.get("status")

    if status == "ready":
        _set(db, pending_start=False)
        started = backends.session_start()
        return _compose(
            started,
            triggered=bool(started.get("triggered", True)),
            dataset=_dataset_view(ds),
        )

    if status == "failed":
        _set(db, pending_start=False)
        raise SessionError(f"Preparing the model dataset failed: {ds.get('error') or 'unknown error'}")

    # preparing (the Hugging Face -> Kaggle run is under way)
    _set(db, pending_start=True, pending_since=_now())
    return _blank("preparing_dataset", triggered=True, dataset=_dataset_view(ds))


def _status(db) -> dict:
    note = None
    doc = _doc(db)

    if doc.get("pending_start"):
        since = _as_utc(doc.get("pending_since"))
        if since and _now() - since > _pending_max():
            _set(db, pending_start=False)
            note = "Gave up waiting for the model dataset to be prepared."
        else:
            return _advance_pending(db)

    out = _compose(backends.session_status())
    if note and not out["error"]:
        out["error"] = note
    return out


def _advance_pending(db) -> dict:
    ds = backends.dataset_status()
    status = ds.get("status")

    if status == "ready":
        if _claim_pending(db):
            started = backends.session_start()
            return _compose(
                started,
                triggered=bool(started.get("triggered", True)),
                dataset=_dataset_view(ds),
            )
        # Another poller just started it.
        return _compose(backends.session_status(), dataset=_dataset_view(ds))

    if status == "failed":
        _set(db, pending_start=False)
        return _blank(
            "error",
            error=f"Preparing the model dataset failed: {ds.get('error') or 'unknown error'}",
            dataset=_dataset_view(ds),
        )

    if status == "missing":
        # Nothing running and nothing on Kaggle: the dataset backend lost track
        # of the run (e.g. its state was reset). Kick it off again.
        ds = backends.dataset_ensure()

    return _blank("preparing_dataset", dataset=_dataset_view(ds))
