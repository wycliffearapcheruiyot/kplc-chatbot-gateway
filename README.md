> **Part of the [KPLC Chatbot System](https://github.com/wycliffearapcheruiyot/kplc-chatbot-system).**
> This repo: FastAPI router — the one service both frontends call
> Sibling repos: [kplc-chatbot-web](https://github.com/wycliffearapcheruiyot/kplc-chatbot-web), [kplc-chatbot-admin](https://github.com/wycliffearapcheruiyot/kplc-chatbot-admin), [kplc-chatbot-inference](https://github.com/wycliffearapcheruiyot/kplc-chatbot-inference), [kplc-chatbot-dataset-sync](https://github.com/wycliffearapcheruiyot/kplc-chatbot-dataset-sync), [kplc-chatbot-kb-builder](https://github.com/wycliffearapcheruiyot/kplc-chatbot-kb-builder), [kplc-chatbot-db-infra](https://github.com/wycliffearapcheruiyot/kplc-chatbot-db-infra)

# Kenya Power Chatbot — Gateway (app backend)

The one service the frontends talk to. It connects three things:

```
Next.js chatbot (Vercel) --+
                           +--> GATEWAY (this repo) --+--> session backend  (Render)  starts Kaggle, proxies /generate + /embed
React admin (Netlify) -----+                          +--> dataset backend  (Render)  makes sure the model files exist on Kaggle
                                                      +--> MongoDB Atlas              chunks, chat_logs, pending-start flag
```

| Service | URL |
|---|---|
| Session backend | `https://kplc-kaggle-notebook-instance.onrender.com` |
| Dataset backend | `https://dataset-trigger-backend.onrender.com` |
| Gateway | *(this service — deploy it, see below)* |

**What "Start demo" does now**

1. Gateway asks the dataset backend whether the model dataset is on Kaggle.
   - Present → go to step 2.
   - Missing → dataset backend builds it from Hugging Face; the gateway reports
     `preparing_dataset` and, while the frontend polls `/session/status`, starts
     the session the moment the dataset is ready.
2. Gateway tells the session backend to start the Kaggle notebook (`starting`).
3. The notebook calls the **session backend's** webhooks (not this service) when
   the model + tunnel are up → `ready`. Chat questions then flow
   gateway → session backend → Cloudflare tunnel → Kaggle.

The gateway holds no Kaggle credentials, no tunnel URL and no webhook secrets.
Its only state is a small "waiting for the dataset" flag in MongoDB, so a
restart or sleep can't lose a start request.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app: endpoints, CORS, admin auth, retrieval, chat |
| `session_manager.py` | Start-demo orchestration (dataset gate → session start) |
| `backends.py` | HTTP clients for the session backend and dataset backend |
| `settings_catalog.py` | Every env var of every service (type, default, secret, when it applies) + validation |
| `settings_routes.py` | `GET /settings`, `PUT /settings/{service}` for the admin panel's Environment tab |
| `app_config.py`, `runtime_config.py` | Settings lookup: admin-panel override → env var → default (shared code, same file in every Python service) |
| `model_client.py` | `generate()` / `embed()`, via the session backend |
| `system_prompt.md` | System prompt with the `{{KNOWLEDGE_BASE}}` placeholder |
| `API.md` | **Endpoint contract for the two frontends** |
| `render.yaml` | Optional Render Blueprint |
| `tests/` | 60 tests against fake versions of both backends (`pytest tests`) |

## Editing environment variables from the admin panel

The admin panel's **Environment** tab edits the variables of every service
(gateway, session backend, dataset backend, KB builder). Edits are stored in
MongoDB (`kplc_chatbot.service_settings`, one document per service) and each
service re-reads them every ~10 s, so no redeploy is needed.

- **Precedence:** panel override → real environment variable → built-in default.
  *Reset to env* in the panel deletes the override.
- **Still real env vars:** `MONGODB_URI` (it is how a service finds the database), `MONGODB_DB`
  on the dataset backend, `PYTHON_VERSION`. The panel shows them read-only.
- **Every Python service needs `MONGODB_URI`** (the same cluster) so it can read its settings,
  including the session backend, which had no database before. If MongoDB is unreachable it keeps
  the last known values, or falls back to plain env vars.
- **Secrets are shown in full** to anyone holding `ADMIN_TOKEN`, and are stored unencrypted in
  Atlas. Treat `ADMIN_TOKEN` (and Atlas access) as the key to every service.
- **Locked out?** Delete the key from `values` of that service's document in Atlas (or unset the
  override), and the real env var applies again.

## Deploy on Render

1. Push this folder to GitHub (`.env` is git-ignored — never commit it).
2. Render → **New → Web Service** (or **New → Blueprint** to use `render.yaml`).
   Build: `pip install -r requirements.txt` · Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Environment (see `.env.example`):

   | Variable | Value |
   |---|---|
   | `MONGODB_URI` | Atlas connection string |
   | `SESSION_BACKEND_URL` | `https://kplc-kaggle-notebook-instance.onrender.com` |
   | `DATASET_BACKEND_URL` | `https://dataset-trigger-backend.onrender.com` |
   | `DATASET_TRIGGER_SECRET` | same value as on the dataset backend |
   | `ADMIN_TOKEN` | `openssl rand -hex 32` |
   | `ALLOWED_ORIGINS` | exact Vercel + Netlify URLs, comma-separated, no trailing `/` |
   | `PYTHON_VERSION` | `3.12.7` |

4. Verify the connections (first call can take a minute while free-tier services wake):
   ```bash
   curl https://YOUR-GATEWAY.onrender.com/health/upstreams
   # {"mongodb":true,"session_backend":true,"dataset_backend":true}
   ```
5. Try the flow:
   ```bash
   curl -X POST https://YOUR-GATEWAY.onrender.com/session/start
   curl https://YOUR-GATEWAY.onrender.com/session/status        # repeat until "ready"
   curl -X POST https://YOUR-GATEWAY.onrender.com/chunks/embed -H "X-Admin-Token: $ADMIN_TOKEN"
   curl -X POST https://YOUR-GATEWAY.onrender.com/chat -H "Content-Type: application/json" \
        -d '{"question":"How do I buy tokens?"}'
   ```
   (On Windows Git Bash add `--ssl-no-revoke` if curl reports a certificate-revocation error.)

Run with a **single worker** (the default above): the retrieval cache is
per-process, so extra workers would each hold their own copy.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env      # fill in real values
uvicorn main:app --reload --port 8000
pip install -r requirements-dev.txt && pytest tests     # optional
```

## Behaviour worth knowing
- Retrieval needs a **ready** session: questions are embedded on the Kaggle GPU.
  With no session, `/chat` replies `session_not_active` (HTTP 200) instead of erroring.
- Editing a chunk clears its embedding; it stays out of retrieval until
  `POST /chunks/embed` runs against a ready session.
- Free-tier cold starts stack: gateway, session backend and dataset backend can
  each take 30–60 s to wake on the first request after a quiet period.
