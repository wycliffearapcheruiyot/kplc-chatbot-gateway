"""
backends.py

Thin HTTP clients for the two services this gateway sits in front of:

  * session backend  (kplc-kaggle-notebook-instance on Render)
        Starts/stops the Kaggle GPU session, receives the notebook's
        webhooks, and proxies /generate and /embed to the model.
  * dataset backend  (dataset-trigger-backend on Render)
        Makes sure the model-weights dataset exists on Kaggle, building it
        from Hugging Face when it doesn't.

Settings are read at call time through app_config.cfg: an override saved in the
admin panel wins, then the real env var, then the default. A local .env that
main.py loads afterwards is still honoured.

  SESSION_BACKEND_URL      e.g. https://kplc-kaggle-notebook-instance.onrender.com
  DATASET_BACKEND_URL      optional; if unset, the dataset check is skipped
  DATASET_TRIGGER_SECRET   required when DATASET_BACKEND_URL is set
  UPSTREAM_TIMEOUT_SECONDS default 90 (free-tier services take 30-60s to wake)
"""

import requests

from app_config import cfg


class UpstreamError(Exception):
    """An upstream service was unreachable or returned an unusable answer."""


def session_backend_url() -> str:
    return cfg.get_str("SESSION_BACKEND_URL").rstrip("/")


def dataset_backend_url() -> str:
    return cfg.get_str("DATASET_BACKEND_URL").rstrip("/")


def dataset_gate_enabled() -> bool:
    return bool(dataset_backend_url())


def _timeout() -> int:
    return cfg.get_int("UPSTREAM_TIMEOUT_SECONDS", 90)


def _detail(resp: requests.Response) -> str:
    """Best short human-readable reason from an error response."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])[:500]
    except ValueError:
        pass
    return resp.text[:300]


def _call(method: str, url: str, what: str, *, headers=None, params=None) -> requests.Response:
    try:
        return requests.request(method, url, headers=headers, params=params, timeout=_timeout())
    except requests.exceptions.RequestException as e:
        raise UpstreamError(f"Could not reach the {what} at {url}: {e}") from e


def _json(resp: requests.Response, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError as e:
        raise UpstreamError(
            f"The {what} returned a non-JSON response ({resp.status_code}): {resp.text[:300]}"
        ) from e
    if not isinstance(data, dict):
        raise UpstreamError(f"The {what} returned an unexpected response: {str(data)[:300]}")
    return data


# --- session backend ---------------------------------------------------------

def _session_base() -> str:
    base = session_backend_url()
    if not base:
        raise UpstreamError(
            "SESSION_BACKEND_URL is not set. It should be the URL of the session "
            "backend, e.g. https://kplc-kaggle-notebook-instance.onrender.com"
        )
    return base


def session_status() -> dict:
    """{"state": idle|starting|ready|ended|error, started_at, ready_at, ended_at, error_detail}"""
    resp = _call("GET", f"{_session_base()}/session/status", "session backend")
    if resp.status_code != 200:
        raise UpstreamError(f"The session backend returned {resp.status_code}: {_detail(resp)}")
    return _json(resp, "session backend")


def session_start() -> dict:
    """Asks the session backend to push the Kaggle kernel. Idempotent on its
    side: returns {"triggered": false, ...} if a session is already live."""
    resp = _call("POST", f"{_session_base()}/session/start", "session backend")
    if resp.status_code != 200:
        raise UpstreamError(
            f"The session backend could not start a Kaggle session ({resp.status_code}): {_detail(resp)}"
        )
    return _json(resp, "session backend")


# --- dataset backend ---------------------------------------------------------

def _dataset_call(method: str, path: str, params=None) -> dict:
    base = dataset_backend_url()
    secret = cfg.get_str("DATASET_TRIGGER_SECRET")
    if not base:
        raise UpstreamError("DATASET_BACKEND_URL is not set.")
    if not secret:
        raise UpstreamError("DATASET_TRIGGER_SECRET is not set (needed to call the dataset backend).")

    resp = _call(
        method, f"{base}{path}", "dataset backend",
        headers={"X-Webhook-Secret": secret}, params=params,
    )
    # 200 (ready / missing), 202 (preparing) and 502 (failed) all carry a
    # {"status": ...} body. Anything else (401, 500, 503...) is a real error.
    if resp.status_code in (200, 202, 502):
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "status" in data:
            return data
    raise UpstreamError(f"The dataset backend returned {resp.status_code}: {_detail(resp)}")


def dataset_ensure() -> dict:
    """Is the model dataset on Kaggle? If not, starts the Hugging Face ->
    Kaggle run. Never blocks (wait=0). Returns {"status": ready|preparing|failed, ...}."""
    return _dataset_call("POST", "/dataset/ensure", params={"wait": 0})


def dataset_status() -> dict:
    """Same answer as dataset_ensure() but never starts a run."""
    return _dataset_call("GET", "/dataset/status")


# --- health ------------------------------------------------------------------

def ping(base_url: str) -> bool:
    if not base_url:
        return False
    try:
        return requests.get(f"{base_url}/health", timeout=_timeout()).status_code == 200
    except requests.exceptions.RequestException:
        return False
