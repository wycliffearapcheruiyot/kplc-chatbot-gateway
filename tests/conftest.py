import os, sys, time, subprocess, threading
import pytest, requests

GW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GW)
sys.path.insert(0, os.path.dirname(__file__))

SESSION_PORT, DATASET_PORT = 9101, 9102

os.environ["MONGODB_URI"] = "mongodb://fake"
os.environ["SESSION_BACKEND_URL"] = f"http://127.0.0.1:{SESSION_PORT}"
os.environ["DATASET_BACKEND_URL"] = f"http://127.0.0.1:{DATASET_PORT}"
os.environ["DATASET_TRIGGER_SECRET"] = "ds-secret"
os.environ["ADMIN_TOKEN"] = "admin-tok"
os.environ["ALLOWED_ORIGINS"] = "https://chat.vercel.app/,https://admin.netlify.app"
os.environ["UPSTREAM_TIMEOUT_SECONDS"] = "5"
os.environ["GENERATE_TIMEOUT_SECONDS"] = "5"
os.environ["EMBED_TIMEOUT_SECONDS"] = "5"

# in-memory Mongo, patched in BEFORE main is imported
import mongomock, pymongo
_mock = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _mock

import uvicorn

def _serve(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5); return
        except Exception: time.sleep(0.1)
    raise RuntimeError(f"fake on {port} did not start")

import fakes
_serve(fakes.session, SESSION_PORT)
_serve(fakes.dataset, DATASET_PORT)

@pytest.fixture(scope="session")
def main_mod():
    import main
    return main

@pytest.fixture()
def client(main_mod):
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        yield c

@pytest.fixture(autouse=True)
def reset(main_mod):
    # fresh upstream + mongo state for every test
    fakes.S.update(state="idle", start_calls=0, start_hits=0, error_detail=None, fail_start=False)
    fakes.D.update(status="missing", error=None, ensure_calls=0)
    main_mod.db["session_state"].delete_many({})
    main_mod.db["chat_logs"].delete_many({})
    main_mod.db["chunks"].delete_many({})
    main_mod.db["service_settings"].delete_many({})
    from app_config import cfg
    cfg.invalidate()
    os.environ["SESSION_BACKEND_URL"] = f"http://127.0.0.1:{SESSION_PORT}"
    os.environ["ADMIN_TOKEN"] = "admin-tok"
    yield
