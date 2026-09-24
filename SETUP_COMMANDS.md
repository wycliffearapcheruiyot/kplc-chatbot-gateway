# Setup Commands: Backend — Run Locally, Then Push to GitHub

Run these in order, from inside the `backend_setup/` folder.

## 1. Install and run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Leave that running, and in a second terminal:

```bash
curl http://localhost:8000/health
curl -H "X-Admin-Token: <your ADMIN_TOKEN>" http://localhost:8000/chunks
curl http://localhost:8000/session/status
```

Expected:
- `/health` → `{"status":"ok"}`
- `/chunks` → a JSON array of 36 chunks
- `/session/status` → `{"status":"idle","state":"idle",...}` (needs `SESSION_BACKEND_URL`)

`/session/start`, `/chat`, and `/chunks/embed` need the session backend and
dataset backend (see README) -- set `SESSION_BACKEND_URL` and friends in `.env`
first. This step just confirms the gateway itself and the Mongo connection work.

---

## 2. Push to GitHub with `gh`

```bash
# One-time only, if you haven't already authenticated gh on this machine
gh auth login
```

```bash
# Fix the commit author (it was committed with a placeholder identity)
git config user.email "your-real-email@example.com"
git config user.name "Your Name"
git commit --amend --reset-author -q -m "Initial commit: FastAPI backend"
```

```bash
# Sanity check BEFORE pushing — confirm .env is NOT in this list
git show --stat HEAD
```

```bash
# Create the repo, add it as origin, and push — all in one step
gh repo create kplc-chatbot-backend --public --source=. --remote=origin --push
```

```bash
gh repo view --web
```

---

## Notes

- `.env` holds your real Atlas connection string and is git-ignored -- it stays
  local, never pushed. Only `.env.example` goes to GitHub.
- On Render, set every variable from `.env.example` as a real environment
  variable (see README, "Deploy on Render") -- don't upload `.env`.
- This service no longer needs `KAGGLE_USERNAME`, `KAGGLE_KEY`,
  `KAGGLE_KERNEL_DIR`, `MODEL_TUNNEL_URL` or `SESSION_WEBHOOK_SECRET`: those
  live on the session backend. You can delete them from `.env` and Render.
