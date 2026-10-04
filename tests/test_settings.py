import os

import pytest

ADMIN = {"X-Admin-Token": "admin-tok"}


def put(client, service, body, headers=None):
    return client.put(f"/settings/{service}", json=body, headers={**ADMIN, **(headers or {})})


def var(service_view, name):
    return next(v for v in service_view["vars"] if v["name"] == name)


def service(client, sid):
    r = client.get("/settings", headers=ADMIN)
    assert r.status_code == 200
    return next(s for s in r.json()["services"] if s["id"] == sid)


# ---------- auth ----------
def test_settings_require_admin_token(client):
    assert client.get("/settings").status_code == 401
    assert client.get("/settings", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.put("/settings/gateway", json={"set": {}}).status_code == 401


# ---------- reading ----------
def test_get_lists_every_service_with_full_values(client):
    r = client.get("/settings", headers=ADMIN).json()
    ids = [s["id"] for s in r["services"]]
    assert ids == ["gateway", "session-backend", "dataset-backend", "kb-builder", "db-infra", "chatbot-web", "admin"]

    gw = service(client, "gateway")
    # the gateway reports its own real env, secrets included, in full
    assert var(gw, "ADMIN_TOKEN")["env"] == "admin-tok"
    assert var(gw, "DATASET_TRIGGER_SECRET")["env"] == "ds-secret"
    assert var(gw, "ADMIN_TOKEN")["has_override"] is False
    assert var(gw, "ADMIN_TOKEN")["secret"] is True
    assert gw["env_reported_at"] is not None
    # an unset var is reported as null, not as an empty string
    assert var(gw, "DATASET_PENDING_MAX_MINUTES")["env"] is None


def test_build_time_vars_are_read_only(client):
    assert var(service(client, "gateway"), "MONGODB_URI")["editable"] is False
    assert var(service(client, "chatbot-web"), "NEXT_PUBLIC_API_URL")["editable"] is False
    assert var(service(client, "admin"), "VITE_API_URL")["editable"] is False


# ---------- writing ----------
def test_put_stores_override_and_reset_removes_it(client):
    r = put(client, "gateway", {"set": {"UPSTREAM_TIMEOUT_SECONDS": "42"}})
    assert r.status_code == 200
    v = var(r.json(), "UPSTREAM_TIMEOUT_SECONDS")
    assert v["override"] == "42" and v["has_override"] is True
    assert var(service(client, "gateway"), "UPSTREAM_TIMEOUT_SECONDS")["override"] == "42"

    r = put(client, "gateway", {"reset": ["UPSTREAM_TIMEOUT_SECONDS"]})
    assert var(r.json(), "UPSTREAM_TIMEOUT_SECONDS")["has_override"] is False


def test_empty_string_is_a_real_override(client):
    # e.g. clearing DATASET_BACKEND_URL switches the dataset check off
    r = put(client, "gateway", {"set": {"DATASET_BACKEND_URL": ""}})
    v = var(r.json(), "DATASET_BACKEND_URL")
    assert v["has_override"] is True and v["override"] == ""


def test_set_and_reset_of_same_name_does_not_conflict(client):
    r = put(client, "gateway", {"set": {"EMBED_TIMEOUT_SECONDS": "7"}, "reset": ["EMBED_TIMEOUT_SECONDS"]})
    assert r.status_code == 200
    assert var(r.json(), "EMBED_TIMEOUT_SECONDS")["override"] == "7"


def test_other_services_can_be_edited_too(client):
    r = put(client, "session-backend", {"set": {"AUTO_KEEP_ALIVE": "true", "KEEP_ALIVE_WINDOW_UTC": "05:00-13:00"}})
    assert r.status_code == 200
    assert var(r.json(), "KEEP_ALIVE_WINDOW_UTC")["override"] == "05:00-13:00"
    # the gateway's own document is untouched
    assert all(not v["has_override"] for v in service(client, "gateway")["vars"])


# ---------- validation ----------
@pytest.mark.parametrize("sid,name,value", [
    ("gateway", "UPSTREAM_TIMEOUT_SECONDS", "ninety"),
    ("gateway", "UPSTREAM_TIMEOUT_SECONDS", "-5"),
    ("gateway", "SESSION_BACKEND_URL", "not a url"),
    ("gateway", "ALLOWED_ORIGINS", "https://a.app/path"),
    ("gateway", "ADMIN_TOKEN", "short"),
    ("gateway", "ADMIN_TOKEN", ""),
    ("session-backend", "AUTO_KEEP_ALIVE", "maybe"),
    ("session-backend", "KAGGLE_ACCOUNTS", "{oops"),
    ("session-backend", "KAGGLE_ACCOUNTS", "42"),
    ("session-backend", "KEEP_ALIVE_WINDOW_UTC", "25:00-13:00"),
    ("session-backend", "KEEP_ALIVE_WINDOW_UTC", "morning"),
    ("session-backend", "RESTART_EVERY_HOURS", "0"),
])
def test_invalid_values_are_rejected_and_nothing_is_saved(client, sid, name, value):
    r = put(client, sid, {"set": {name: value}})
    assert r.status_code == 422 and name in r.json()["detail"]
    assert not var(service(client, sid), name)["has_override"]


def test_unknown_service_variable_and_read_only_are_rejected(client):
    assert put(client, "nope", {"set": {"X": "1"}}).status_code == 404
    assert put(client, "gateway", {"set": {"NOT_A_VAR": "1"}}).status_code == 422
    assert put(client, "gateway", {"set": {"MONGODB_URI": "mongodb://x"}}).status_code == 422
    assert put(client, "gateway", {"set": {"SESSION_BACKEND_URL": "http://x"}, "reset": ["MONGODB_URI"]}).status_code == 422
    # a var of another service is not accepted here
    assert put(client, "gateway", {"set": {"KAGGLE_KEY": "k"}}).status_code == 422


def test_one_bad_value_saves_nothing(client):
    r = put(client, "gateway", {"set": {"EMBED_TIMEOUT_SECONDS": "10", "GENERATE_TIMEOUT_SECONDS": "x"}})
    assert r.status_code == 422
    assert not var(service(client, "gateway"), "EMBED_TIMEOUT_SECONDS")["has_override"]


# ---------- the overrides actually change behaviour (gateway) ----------
def test_override_wins_over_env_and_reset_falls_back(client):
    import backends
    assert backends.session_backend_url() == os.environ["SESSION_BACKEND_URL"]
    put(client, "gateway", {"set": {"SESSION_BACKEND_URL": "https://override.example.com/"}})
    assert backends.session_backend_url() == "https://override.example.com"
    put(client, "gateway", {"reset": ["SESSION_BACKEND_URL"]})
    assert backends.session_backend_url() == os.environ["SESSION_BACKEND_URL"]


def test_timeout_override_is_used(client):
    import backends
    put(client, "gateway", {"set": {"UPSTREAM_TIMEOUT_SECONDS": "33"}})
    assert backends._timeout() == 33


def test_empty_dataset_url_override_disables_the_dataset_gate(client):
    import backends
    assert backends.dataset_gate_enabled() is True
    put(client, "gateway", {"set": {"DATASET_BACKEND_URL": ""}})
    assert backends.dataset_gate_enabled() is False


def test_admin_token_can_be_rotated_from_the_panel(client):
    new = "a-brand-new-admin-token-123"
    assert put(client, "gateway", {"set": {"ADMIN_TOKEN": new}}).status_code == 200
    assert client.get("/chunks", headers=ADMIN).status_code == 401           # old token is dead
    assert client.get("/chunks", headers={"X-Admin-Token": new}).status_code == 200


def test_allowed_origins_apply_live_to_cors(client):
    def preflight(origin):
        return client.options("/chat", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})

    assert "access-control-allow-origin" not in preflight("https://new.example.com").headers
    r = put(client, "gateway", {"set": {"ALLOWED_ORIGINS": "https://new.example.com/, https://admin.netlify.app"}},
            headers={"Origin": "https://admin.netlify.app"})
    assert r.status_code == 200
    assert preflight("https://new.example.com").headers["access-control-allow-origin"] == "https://new.example.com"
    assert "access-control-allow-origin" not in preflight("https://chat.vercel.app").headers  # no longer listed
    assert preflight("http://localhost:3000").headers["access-control-allow-origin"] == "http://localhost:3000"


def test_allowed_origins_cannot_lock_out_the_admin_panel(client):
    r = put(client, "gateway", {"set": {"ALLOWED_ORIGINS": "https://only-the-chatbot.app"}},
            headers={"Origin": "https://admin.netlify.app"})
    assert r.status_code == 422 and "admin.netlify.app" in r.json()["detail"]
    assert not var(service(client, "gateway"), "ALLOWED_ORIGINS")["has_override"]


# ---------- runtime_config unit behaviour ----------
def test_runtime_config_falls_back_to_env_when_mongo_fails():
    import runtime_config

    class Boom:
        def __getitem__(self, _):
            raise RuntimeError("mongo down")

    c = runtime_config.make("x", ["A"])
    c.bind(Boom())
    os.environ["RC_TEST"] = "from-env"
    try:
        assert c.get("RC_TEST") == "from-env"
        assert c.get("RC_MISSING", "dflt") == "dflt"
    finally:
        del os.environ["RC_TEST"]


def test_runtime_config_typed_getters_fall_back_on_garbage(main_mod):
    import runtime_config
    c = runtime_config.make("gateway")
    c.bind(main_mod.db)
    main_mod.db["service_settings"].update_one(
        {"_id": "gateway"}, {"$set": {"values.N": "abc", "values.F": "", "values.B": "On"}}, upsert=True)
    c.invalidate()
    assert c.get_int("N", 5) == 5
    assert c.get_float("F", 1.5) == 1.5
    assert c.get_bool("B") is True
    assert c.get_bool("NOPE", True) is True
    assert c.environ()["N"] == "abc"
