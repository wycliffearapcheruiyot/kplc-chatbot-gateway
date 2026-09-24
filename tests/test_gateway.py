import os
import fakes

ADMIN = {"X-Admin-Token": "admin-tok"}

def seed_chunks(main_mod, embedded=True):
    docs = []
    for i, (topic, text) in enumerate([("Buying tokens", "Buy tokens via M-Pesa Paybill 888880"),
                                        ("Outages", "Report outages on 97771"),
                                        ("Connections", "Apply for a new connection online")], 1):
        d = {"id": f"kb-00{i}", "topic": topic, "text": text, "section": "s", "metadata": {}}
        if embedded: d["embedding"] = fakes.vec(text)
        docs.append(d)
    main_mod.db["chunks"].insert_many(docs)
    main_mod.load_chunk_embeddings()

# ---------- health / info ----------
def test_root_and_health(client):
    assert client.get("/").json()["docs"] == "/docs"
    assert client.get("/health").json() == {"status": "ok"}

def test_health_upstreams_all_up(client):
    r = client.get("/health/upstreams").json()
    assert r == {"mongodb": True, "session_backend": True, "dataset_backend": True}

def test_health_upstreams_reports_down(client):
    os.environ["SESSION_BACKEND_URL"] = "http://127.0.0.1:1"
    assert client.get("/health/upstreams").json()["session_backend"] is False

# ---------- CORS ----------
def test_cors_trailing_slash_origin_is_accepted(client):
    r = client.options("/chat", headers={"Origin": "https://chat.vercel.app",
        "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert r.headers.get("access-control-allow-origin") == "https://chat.vercel.app"

def test_cors_unknown_origin_rejected(client):
    r = client.options("/chat", headers={"Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers

# ---------- session: dataset already ready ----------
def test_status_idle(client):
    r = client.get("/session/status").json()
    assert r["status"] == "idle" and r["state"] == "idle"

def test_start_when_dataset_ready(client):
    fakes.D["status"] = "ready"
    r = client.post("/session/start").json()
    assert r["status"] == "starting" and r["triggered"] is True
    assert fakes.S["start_calls"] == 1
    # second click does not push again, and does not even bother the dataset backend
    calls_before = fakes.D["ensure_calls"]
    r2 = client.post("/session/start").json()
    assert r2["status"] == "starting" and r2["triggered"] is False
    assert fakes.S["start_calls"] == 1
    assert fakes.D["ensure_calls"] == calls_before

def test_status_maps_ended_to_idle_and_error_to_error(client):
    fakes.S["state"] = "ended"
    assert client.get("/session/status").json()["status"] == "idle"
    fakes.S.update(state="error", error_detail="kaggle push failed")
    r = client.get("/session/status").json()
    assert r["status"] == "error" and r["error"] == "kaggle push failed"

# ---------- session: dataset must be built first ----------
def test_start_waits_for_dataset_then_advances_on_poll(client, main_mod):
    # dataset missing -> ensure kicks off a build -> "preparing"
    r = client.post("/session/start").json()
    assert r["status"] == "preparing_dataset" and r["triggered"] is True
    assert fakes.S["start_calls"] == 0                      # kernel NOT pushed yet
    assert main_mod.db["session_state"].find_one({"_id": "session"})["pending_start"] is True

    # still building: polls stay in preparing_dataset and never touch the session
    for _ in range(3):
        assert client.get("/session/status").json()["status"] == "preparing_dataset"
    assert fakes.S["start_calls"] == 0
    assert fakes.D["ensure_calls"] == 1                      # only ONE build was started

    # dataset finishes -> the next poll starts the session
    fakes.D["status"] = "ready"
    r = client.get("/session/status").json()
    assert r["status"] == "starting" and fakes.S["start_calls"] == 1
    assert main_mod.db["session_state"].find_one({"_id": "session"})["pending_start"] is False

def test_click_again_while_preparing_does_not_start_second_build(client):
    client.post("/session/start")
    r = client.post("/session/start").json()
    assert r["status"] == "preparing_dataset" and r["triggered"] is False
    assert fakes.D["ensure_calls"] == 1

def test_two_pollers_start_the_session_only_once(client, main_mod):
    client.post("/session/start")
    fakes.D["status"] = "ready"
    a = client.get("/session/status").json()
    b = client.get("/session/status").json()
    assert a["status"] == "starting" and b["status"] == "starting"
    assert fakes.S["start_calls"] == 1

def test_dataset_build_failure_on_start_is_502(client):
    fakes.D.update(status="failed", error="HF download failed")
    r = client.post("/session/start")
    assert r.status_code == 502 and "HF download failed" in r.json()["detail"]
    assert fakes.S["start_calls"] == 0

def test_dataset_build_failure_while_waiting_shows_error_and_clears_pending(client, main_mod):
    client.post("/session/start")
    fakes.D.update(status="failed", error="Timed out")
    r = client.get("/session/status").json()
    assert r["status"] == "error" and "Timed out" in r["error"]
    assert main_mod.db["session_state"].find_one({"_id": "session"})["pending_start"] is False
    assert client.get("/session/status").json()["status"] == "idle"   # back to normal; can retry

def test_dataset_backend_forgot_run_is_restarted(client):
    client.post("/session/start")            # preparing, ensure_calls == 1
    fakes.D["status"] = "missing"            # e.g. its Mongo doc was wiped
    assert client.get("/session/status").json()["status"] == "preparing_dataset"
    assert fakes.D["ensure_calls"] == 2

def test_pending_expires(client, main_mod):
    from datetime import datetime, timedelta, timezone
    client.post("/session/start")
    main_mod.db["session_state"].update_one({"_id": "session"},
        {"$set": {"pending_since": datetime.now(timezone.utc) - timedelta(minutes=500)}})
    r = client.get("/session/status").json()
    assert r["status"] == "idle" and "Gave up" in r["error"]

def test_session_backend_push_failure_is_502(client):
    fakes.D["status"] = "ready"; fakes.S["fail_start"] = True
    r = client.post("/session/start")
    assert r.status_code == 502 and "boom" in r.json()["detail"]

def test_session_backend_unreachable_is_502_with_clear_message(client):
    os.environ["SESSION_BACKEND_URL"] = "http://127.0.0.1:1"
    r = client.get("/session/status")
    assert r.status_code == 502 and "Could not reach the session backend" in r.json()["detail"]

def test_dataset_backend_wrong_secret_is_reported(client):
    os.environ["DATASET_TRIGGER_SECRET"] = "wrong"
    try:
        r = client.post("/session/start")
        assert r.status_code == 502 and "401" in r.json()["detail"]
    finally:
        os.environ["DATASET_TRIGGER_SECRET"] = "ds-secret"

def test_dataset_gate_can_be_disabled(client):
    saved = os.environ.pop("DATASET_BACKEND_URL")
    try:
        r = client.post("/session/start").json()
        assert r["status"] == "starting" and fakes.D["ensure_calls"] == 0
    finally:
        os.environ["DATASET_BACKEND_URL"] = saved

# ---------- chat ----------
def test_chat_without_session_is_friendly_not_an_error(client, main_mod):
    seed_chunks(main_mod)
    r = client.post("/chat", json={"question": "How do I buy tokens?"})
    assert r.status_code == 200
    assert r.json()["error"] == "session_not_active" and r.json()["answer"] is None
    assert main_mod.db["chat_logs"].count_documents({}) == 0

def test_chat_with_session_retrieves_answers_and_logs(client, main_mod):
    seed_chunks(main_mod)
    fakes.S["state"] = "ready"
    r = client.post("/chat", json={"question": "Buy tokens via M-Pesa Paybill 888880"})
    assert r.status_code == 200
    assert r.json()["answer"].startswith("ANSWER to")
    assert "prompt_has_kb=True" in r.json()["answer"]          # retrieved chunks made it into the prompt
    log = main_mod.db["chat_logs"].find_one({})
    assert log["retrieved_chunk_ids"][0] == "kb-001"           # exact-match text ranked first
    assert len(log["retrieved_chunk_ids"]) == 3

def test_chat_with_no_embeddings_still_answers_without_context(client, main_mod):
    seed_chunks(main_mod, embedded=False)
    fakes.S["state"] = "ready"
    r = client.post("/chat", json={"question": "hello"})
    assert r.json()["answer"].startswith("ANSWER") and "prompt_has_kb=False" in r.json()["answer"]

def test_chat_validation(client):
    assert client.post("/chat", json={"question": ""}).status_code == 422
    assert client.post("/chat", json={"question": "x" * 2001}).status_code == 422

def test_chat_upstream_down_is_502(client, main_mod):
    seed_chunks(main_mod, embedded=False)
    os.environ["SESSION_BACKEND_URL"] = "http://127.0.0.1:1"
    assert client.post("/chat", json={"question": "hi"}).status_code == 502

# ---------- admin ----------
def test_admin_endpoints_require_token(client):
    for method, path in [("get", "/chunks"), ("put", "/chunks/kb-001"), ("post", "/chunks/embed"), ("get", "/chat_logs")]:
        kw = {"json": {"text": "x"}} if method == "put" else {}
        assert getattr(client, method)(path, **kw).status_code == 401, path
        assert getattr(client, method)(path, headers={"X-Admin-Token": "wrong"}, **kw).status_code == 401, path

def test_admin_fails_closed_without_configured_token(client):
    os.environ["ADMIN_TOKEN"] = ""
    assert client.get("/chunks", headers=ADMIN).status_code == 503

def test_list_chunks_hides_vectors_and_flags_embedded(client, main_mod):
    seed_chunks(main_mod)
    main_mod.db["chunks"].update_one({"id": "kb-002"}, {"$unset": {"embedding": ""}})
    rows = {c["id"]: c for c in client.get("/chunks", headers=ADMIN).json()}
    assert all("embedding" not in c and "_id" not in c for c in rows.values())
    assert rows["kb-001"]["embedded"] is True and rows["kb-002"]["embedded"] is False

def test_edit_chunk_clears_embedding_and_drops_from_retrieval(client, main_mod):
    seed_chunks(main_mod)
    assert len(main_mod._chunk_cache) == 3
    r = client.put("/chunks/kb-001", json={"text": "New wording"}, headers=ADMIN)
    assert r.json()["ok"] is True
    assert "embedding" not in main_mod.db["chunks"].find_one({"id": "kb-001"})
    assert len(main_mod._chunk_cache) == 2
    assert client.put("/chunks/nope", json={"text": "x"}, headers=ADMIN).status_code == 404

def test_embed_needs_session_then_works(client, main_mod):
    seed_chunks(main_mod, embedded=False)
    r = client.post("/chunks/embed", headers=ADMIN)
    assert r.status_code == 409
    fakes.S["state"] = "ready"
    r = client.post("/chunks/embed", headers=ADMIN).json()
    assert r["embedded"] == 3 and len(main_mod._chunk_cache) == 3
    assert client.post("/chunks/embed", headers=ADMIN).json()["embedded"] == 0     # nothing left
    assert client.post("/chunks/embed?force=true", headers=ADMIN).json()["embedded"] == 3

def test_chat_logs(client, main_mod):
    seed_chunks(main_mod); fakes.S["state"] = "ready"
    client.post("/chat", json={"question": "a"}); client.post("/chat", json={"question": "b"})
    logs = client.get("/chat_logs?limit=1", headers=ADMIN).json()
    assert len(logs) == 1 and logs[0]["question"] == "b"          # newest first
    assert client.get("/chat_logs?limit=0", headers=ADMIN).status_code == 422


def test_race_two_pollers_that_both_saw_pending_push_only_once(client, main_mod):
    """Deterministic version of the real race: both requests already read
    pending_start=True; only the atomic claim keeps the second from pushing."""
    import session_manager
    client.post("/session/start")                      # -> preparing, pending_start=True
    fakes.D["status"] = "ready"
    a = session_manager._advance_pending(main_mod.db)  # poller A wins the claim
    b = session_manager._advance_pending(main_mod.db)  # poller B ran the same step concurrently
    assert a["status"] == "starting" and b["status"] == "starting"
    assert fakes.S["start_hits"] == 1                  # the kernel was pushed ONCE
