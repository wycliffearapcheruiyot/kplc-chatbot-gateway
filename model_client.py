"""
model_client.py

Wrapper around the model, reached THROUGH the session backend
(kplc-kaggle-notebook-instance), which owns the Cloudflare Tunnel hostname
and refuses to proxy unless a Kaggle session is ready. Going through it means
this gateway needs no tunnel URL, no Kaggle credentials, and never has to
guess whether a session is alive -- the session backend answers 503 when it
isn't, which becomes SessionNotReady here.

  POST {SESSION_BACKEND_URL}/generate
    request:  {"system_prompt": "...", "question": "..."}
    response: {"answer": "..."}

  POST {SESSION_BACKEND_URL}/embed
    request:  {"texts": ["...", "..."]}
    response: {"embeddings": [[...], [...]]}   # one normalized vector per text

Settings are read at call time through app_config.cfg (admin-panel override,
then env var, then default).
  SESSION_BACKEND_URL
  GENERATE_TIMEOUT_SECONDS   default 150 (covers a cold start + generation)
  EMBED_TIMEOUT_SECONDS      default 150
"""

import requests

from app_config import cfg


class ModelClientError(Exception):
    """The model could not be reached or returned something unusable."""


class SessionNotReady(ModelClientError):
    """The session backend says no model session is running right now."""


def _post(path: str, payload: dict, timeout: int) -> dict:
    base = cfg.get_str("SESSION_BACKEND_URL").rstrip("/")
    if not base:
        raise ModelClientError(
            "SESSION_BACKEND_URL is not set. It should be the URL of the session "
            "backend, e.g. https://kplc-kaggle-notebook-instance.onrender.com"
        )
    url = f"{base}{path}"

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise ModelClientError(f"Could not reach the session backend at {url}: {e}") from e

    # The session backend's own "no session" answer is a 503 whose detail says
    # so. Any other 503 (e.g. a platform hiccup) is a plain error.
    if resp.status_code == 503 and "no model session" in resp.text.lower():
        raise SessionNotReady("No model session is ready.")
    if resp.status_code != 200:
        raise ModelClientError(f"Session backend returned {resp.status_code}: {resp.text[:500]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise ModelClientError(f"Session backend returned non-JSON: {resp.text[:500]}") from e
    if not isinstance(data, dict):
        raise ModelClientError(f"Session backend returned an unexpected response: {str(data)[:500]}")
    return data


def generate(system_prompt: str, question: str) -> str:
    timeout = cfg.get_int("GENERATE_TIMEOUT_SECONDS", 150)
    data = _post("/generate", {"system_prompt": system_prompt, "question": question}, timeout)
    answer = data.get("answer")
    if not answer:
        raise ModelClientError(f"Model response had no 'answer' field: {data}")
    return answer


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    timeout = cfg.get_int("EMBED_TIMEOUT_SECONDS", 150)
    data = _post("/embed", {"texts": texts}, timeout)
    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise ModelClientError(f"Model /embed response was malformed: {data}")
    return embeddings
