"""Fake session backend + fake dataset backend, mimicking the real responses."""
import hashlib, math
from fastapi import FastAPI, Header, HTTPException, Response, Body

# ---------- fake session backend (kplc-kaggle-notebook-instance) ----------
session = FastAPI()
S = {"state": "idle", "start_calls": 0, "start_hits": 0, "error_detail": None, "fail_start": False}

def snap():
    return {"state": S["state"], "started_at": 1.0 if S["state"] != "idle" else None,
            "ready_at": 2.0 if S["state"] == "ready" else None, "ended_at": None,
            "error_detail": S["error_detail"]}

@session.get("/health")
def s_health(): return {"status": "ok"}

@session.get("/session/status")
def s_status(): return snap()

@session.post("/session/start")
def s_start():
    S["start_hits"] += 1
    if S["fail_start"]:
        raise HTTPException(502, "Failed to start Kaggle session: kaggle kernels push failed (exit 1): boom")
    if S["state"] in ("starting", "ready"):
        return {"triggered": False, **snap()}
    S["start_calls"] += 1
    S["state"] = "starting"
    return {"triggered": True, **snap()}

@session.post("/_set")
def s_set(body: dict = Body(...)):
    S.update(body); return snap()

@session.get("/_calls")
def s_calls(): return {"start_calls": S["start_calls"], "start_hits": S["start_hits"]}

def vec(text):
    h = hashlib.sha256(text.lower().encode()).digest()
    v = [b / 255 for b in h[:8]]
    n = math.sqrt(sum(x * x for x in v)); return [x / n for x in v]

def need_ready():
    if S["state"] != "ready":
        raise HTTPException(503, "No model session is ready. POST /session/start first, then poll /session/status until state is 'ready'.")

@session.post("/generate")
def s_generate(body: dict = Body(...)):
    need_ready()
    kb = body["system_prompt"]
    return {"answer": f"ANSWER to '{body['question']}' | prompt_has_kb={'[kb-' in kb}"}

@session.post("/embed")
def s_embed(body: dict = Body(...)):
    need_ready()
    return {"embeddings": [vec(t) for t in body["texts"]]}

# ---------- fake dataset backend (dataset-trigger-backend) ----------
dataset = FastAPI()
D = {"status": "missing", "error": None, "ensure_calls": 0}
SECRET = "ds-secret"

def auth(x):
    if x != SECRET: raise HTTPException(401, "Invalid or missing X-Webhook-Secret.")

def dresp(response):
    response.status_code = {"ready": 200, "preparing": 202, "failed": 502}.get(D["status"], 200)
    out = {"present": D["status"] == "ready", "status": D["status"]}
    if D["status"] == "failed": out["error"] = D["error"]
    return out

@dataset.get("/health")
def d_health(): return {"ok": True}

@dataset.post("/dataset/ensure")
def d_ensure(response: Response, wait: int = 0, x_webhook_secret: str = Header(None)):
    auth(x_webhook_secret)
    D["ensure_calls"] += 1
    if D["status"] in ("missing",):
        D["status"] = "preparing"
    return dresp(response)

@dataset.get("/dataset/status")
def d_status(response: Response, x_webhook_secret: str = Header(None)):
    auth(x_webhook_secret)
    return dresp(response)

@dataset.post("/_set")
def d_set(body: dict = Body(...)):
    D.update(body); return D
