"""
main.py

Gateway / app backend for the Kenya Power chatbot. This is the ONE service the
frontends talk to. It sits in front of two other Render services and MongoDB:

    Next.js chatbot (Vercel) --+
                               +--> THIS GATEWAY --+--> session backend  (starts Kaggle, proxies the model)
    React admin (Netlify) -----+                   +--> dataset backend  (makes sure the model files exist on Kaggle)
                                                   +--> MongoDB Atlas    (chunks, chat_logs, pending-start flag)

What lives here: retrieval over the knowledge chunks, prompt building, chat
logging, the admin endpoints, and the "Start demo" orchestration. What does
NOT: the Kaggle credentials, the Cloudflare tunnel, the webhooks -- those stay
with the session backend, which the Kaggle notebook calls back.

Run locally:
    uvicorn main:app --reload --port 8000

Endpoints (see API.md for request/response shapes):
    GET  /                    service info
    GET  /health              liveness
    GET  /health/upstreams    can this gateway reach MongoDB + both backends?
    POST /session/start       dataset check -> start Kaggle session
    GET  /session/status      idle | preparing_dataset | starting | ready | error
    POST /chat                public
  Admin (header X-Admin-Token):
    GET  /chunks
    PUT  /chunks/{chunk_id}
    POST /chunks/embed
    GET  /chat_logs
"""

import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.server_api import ServerApi

import backends
import model_client
import session_manager

HERE = Path(__file__).resolve().parent


# --- tiny .env loader (no extra dependency; only fills vars that aren't already set) ---
def _load_dotenv_if_present():
    env_path = HERE / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv_if_present()

MONGODB_URI = os.environ.get("MONGODB_URI")
# Origins must match the browser's Origin header exactly, which never has a
# trailing slash -- so strip one if it was pasted in.
ALLOWED_ORIGINS = [
    o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
# Always allow localhost for local dev, in addition to whatever's configured
ALLOWED_ORIGINS += ["http://localhost:3000", "http://127.0.0.1:3000"]

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set (env var or .env file).")

for _name in ("SESSION_BACKEND_URL", "ADMIN_TOKEN"):
    if not os.environ.get(_name):
        print(f"WARNING: env var {_name} is not set. See .env.example.", flush=True)
if backends.dataset_gate_enabled() and not os.environ.get("DATASET_TRIGGER_SECRET"):
    print("WARNING: DATASET_BACKEND_URL is set but DATASET_TRIGGER_SECRET is not.", flush=True)

DB_NAME = "kplc_chatbot"
TOP_K = 3

client = MongoClient(MONGODB_URI, server_api=ServerApi("1"))
db = client[DB_NAME]

SYSTEM_PROMPT_TEMPLATE = (HERE / "system_prompt.md").read_text()

# in-memory cache of {id, section, topic, text, metadata, embedding} -- only
# chunks that already have a precomputed "embedding" field in Mongo make it
# into this cache. Embeddings are written by POST /chunks/embed, which calls
# the model through the session backend; this module never computes one.
_chunk_cache: list[dict] = []


def load_chunk_embeddings():
    """Loads chunks from Mongo into the in-memory cache, keeping only the
    ones that already have an "embedding" (as an ndarray). Chunks without one
    are skipped until POST /chunks/embed fills them in."""
    global _chunk_cache
    docs = list(db["chunks"].find({}, {"_id": 0}))
    _chunk_cache = [
        {**d, "embedding": np.array(d["embedding"], dtype=np.float32)}
        for d in docs
        if d.get("embedding")
    ]


def retrieve(question: str, top_k: int = TOP_K) -> list[dict]:
    """Embeds the question via the model and ranks cached chunk embeddings
    against it. Raises SessionNotReady if no session is running (chat() turns
    that into a friendly reply); any other embedding failure fails soft into
    'no retrieved context' rather than breaking the whole /chat request."""
    if not _chunk_cache:
        return []

    try:
        [q_emb] = model_client.embed([question])
    except model_client.SessionNotReady:
        raise
    except model_client.ModelClientError:
        return []

    q_emb = np.array(q_emb, dtype=np.float32)
    scored = [(float(np.dot(q_emb, c["embedding"])), c) for c in _chunk_cache]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def build_knowledge_base_block(chunks: list[dict]) -> str:
    return "\n\n".join(f"[{c['id']}] {c['topic']}\n{c['text']}" for c in chunks)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_chunk_embeddings()
    yield


# --- app setup ---
app = FastAPI(title="Kenya Power Chatbot Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- admin auth ---
def require_admin(x_admin_token: str | None = Header(default=None)):
    """Guards everything that reads logs or edits the knowledge base. Fails
    closed: with no ADMIN_TOKEN configured, admin endpoints are off."""
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN is not configured on the server, so admin endpoints are disabled.",
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token header.")


# --- schemas ---
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ChunkUpdate(BaseModel):
    text: str = Field(min_length=1)


# --- info / health ---
@app.get("/")
def root():
    return {"service": "Kenya Power Chatbot Gateway", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok"}


def _mongo_ok() -> bool:
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False


@app.get("/health/upstreams")
def health_upstreams():
    """Can this gateway reach everything it depends on? Wakes sleeping
    free-tier services as a side effect, so the first call can take a minute."""
    dataset_url = backends.dataset_backend_url()
    return {
        "mongodb": _mongo_ok(),
        "session_backend": backends.ping(backends.session_backend_url()),
        "dataset_backend": backends.ping(dataset_url) if dataset_url else None,
    }


# --- session lifecycle (orchestrated across the two backends) ---
@app.post("/session/start")
def session_start():
    try:
        return session_manager.start_session(db)
    except session_manager.SessionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/session/status")
def session_status():
    try:
        return session_manager.get_status(db)
    except session_manager.SessionError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- chat ---
@app.post("/chat")
def chat(req: ChatRequest):
    try:
        chunks = retrieve(req.question)
        kb_block = build_knowledge_base_block(chunks)
        filled_prompt = SYSTEM_PROMPT_TEMPLATE.replace("{{KNOWLEDGE_BASE}}", kb_block)
        answer = model_client.generate(filled_prompt, req.question)
    except model_client.SessionNotReady:
        return {
            "answer": None,
            "error": "session_not_active",
            "message": "The model isn't running right now. Click 'Start demo' and wait for it to be ready.",
        }
    except model_client.ModelClientError as e:
        raise HTTPException(status_code=502, detail=str(e))

    db["chat_logs"].insert_one(
        {
            "question": req.question,
            "answer": answer,
            "retrieved_chunk_ids": [c["id"] for c in chunks],
            "timestamp": datetime.now(timezone.utc),
        }
    )

    return {"answer": answer}


# --- admin: chunks ---
@app.get("/chunks", dependencies=[Depends(require_admin)])
def list_chunks():
    """All chunks WITHOUT their embedding vectors (hundreds of floats each the
    admin panel has no use for), plus an `embedded` flag so it can show which
    chunks still need POST /chunks/embed."""
    chunks = list(db["chunks"].find({}, {"_id": 0, "embedding": 0}))
    embedded_ids = {
        d["id"] for d in db["chunks"].find({"embedding": {"$exists": True}}, {"_id": 0, "id": 1})
    }
    for c in chunks:
        c["embedded"] = c.get("id") in embedded_ids
    return chunks


@app.put("/chunks/{chunk_id}", dependencies=[Depends(require_admin)])
def update_chunk(chunk_id: str, update: ChunkUpdate):
    # Text changed, so any existing embedding is stale -- unset it. The chunk
    # then drops out of the retrieval cache until POST /chunks/embed re-embeds it.
    result = db["chunks"].update_one(
        {"id": chunk_id},
        {"$set": {"text": update.text}, "$unset": {"embedding": ""}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"No chunk with id {chunk_id}")
    load_chunk_embeddings()
    return {"ok": True, "id": chunk_id, "note": "embedding cleared -- call POST /chunks/embed to re-embed"}


@app.post("/chunks/embed", dependencies=[Depends(require_admin)])
def embed_chunks(force: bool = False):
    """Embeds chunks through the model and stores the vectors in Mongo.
    Needs a ready session (409 otherwise). By default only chunks missing an
    embedding are done; ?force=true redoes all of them."""
    query = {} if force else {"embedding": {"$exists": False}}
    docs = list(db["chunks"].find(query, {"_id": 0, "id": 1, "text": 1}))
    if not docs:
        return {"embedded": 0, "message": "Nothing to embed."}

    try:
        embeddings = model_client.embed([d["text"] for d in docs])
    except model_client.SessionNotReady:
        raise HTTPException(
            status_code=409,
            detail="No active model session. Start one and wait for 'ready' before embedding chunks.",
        )
    except model_client.ModelClientError as e:
        raise HTTPException(status_code=502, detail=str(e))

    for d, emb in zip(docs, embeddings):
        db["chunks"].update_one({"id": d["id"]}, {"$set": {"embedding": emb}})

    load_chunk_embeddings()
    return {"embedded": len(docs), "ids": [d["id"] for d in docs]}


# --- admin: logs ---
@app.get("/chat_logs", dependencies=[Depends(require_admin)])
def get_chat_logs(limit: int = Query(50, ge=1, le=500)):
    return list(db["chat_logs"].find({}, {"_id": 0}).sort("timestamp", -1).limit(limit))
