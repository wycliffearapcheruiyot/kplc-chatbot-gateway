# Gateway API — contract for the frontends

Base URL: the gateway's Render URL, e.g. `https://kplc-chatbot-gateway.onrender.com`.
Both frontends talk **only** to this service. Set it as an environment variable
in each frontend (`NEXT_PUBLIC_API_URL` on Vercel, `VITE_API_URL` on Netlify) —
this is a public URL, so that's fine. **Never do the same with `ADMIN_TOKEN`**
(see "Admin panel" below).

## Chatbot (Next.js) — public endpoints

### `POST /session/start`
Starts the demo. Safe to call repeatedly / from several tabs: a session that is
already starting or ready is reported, not restarted. Returns the same shape as
`GET /session/status`, plus `"triggered": true|false`.

### `GET /session/status` — poll every 3–5 s
```json
{
  "status": "starting",
  "state": "starting",
  "started_at": 1790236049.3,
  "ready_at": null,
  "ended_at": null,
  "error": null,
  "dataset": { "status": "ready" }
}
```
`status` is the field to switch the UI on:

| `status` | Meaning | UI |
|---|---|---|
| `idle` | Nothing running (also after an idle shutdown) | "Start demo" button |
| `preparing_dataset` | First run only: model files are being copied into Kaggle. Can take several minutes to an hour. | Spinner + "Preparing the model files (first run only)…" |
| `starting` | Kaggle notebook booting / loading the model (~1–3 min) | Spinner |
| `ready` | Chat works | Chat box |
| `error` | Something failed; `error` has the reason | Show `error`, keep "Start demo" so the user can retry |

`dataset` is only present while it's relevant; you can ignore it.

Errors: HTTP **502** with `{"detail": "..."}` means the gateway couldn't reach
or got a failure from one of its backends (`detail` says which and why). Show
it and keep polling — it is often just a service waking up.

### `POST /chat`
Request: `{"question": "How do I buy tokens?"}` (1–2000 characters)

Success: `{"answer": "..."}`

Session not running (HTTP 200, not an error — treat as "go back to idle"):
```json
{ "answer": null, "error": "session_not_active", "message": "The model isn't running right now. ..." }
```
HTTP 502 = the model call itself failed. HTTP 422 = invalid `question`.

### Recommended polling loop
```js
const API = process.env.NEXT_PUBLIC_API_URL;

async function startDemo(setStatus) {
  await fetch(`${API}/session/start`, { method: "POST" });
  for (;;) {
    let s;
    try {
      s = await (await fetch(`${API}/session/status`)).json();
    } catch { s = null; }                       // network blip / service waking up
    if (s?.status) setStatus(s);
    if (s?.status === "ready" || s?.status === "error") return s;
    await new Promise(r => setTimeout(r, 4000));
  }
}
```
Do not put a short fetch timeout on these calls: on Render's free tier the
first request after a quiet period waits 30–60 s per sleeping service (the
gateway, the session backend and the dataset backend can each be asleep).

## Admin panel (React) — needs `X-Admin-Token`

Every request below must send the header `X-Admin-Token: <ADMIN_TOKEN>`.
Missing/wrong → **401**. Server has no `ADMIN_TOKEN` configured → **503**.

**Do not bake the token into the Netlify build** (`VITE_…`/`REACT_APP_…` values
are copied into the public JavaScript). Show a login box, keep the token in
memory (or `sessionStorage`), and send it with each request.

| Call | Purpose |
|---|---|
| `GET /chunks` | All knowledge chunks: `id, section, topic, text, metadata, embedded`. Vectors are not included. `embedded: false` = edited and not yet re-embedded, so invisible to retrieval. |
| `PUT /chunks/{id}` `{"text": "..."}` | Save an edit. Clears that chunk's embedding. 404 if unknown id. |
| `POST /chunks/embed[?force=true]` | (Re-)embed chunks missing an embedding, or all with `force`. Needs a **ready** session: 409 otherwise. Returns `{"embedded": N, "ids": [...]}`. |
| `GET /chat_logs?limit=50` | Newest conversations first (`limit` 1–500). |

Suggested admin flow: after saving a chunk, if `GET /session/status` says
`ready`, call `POST /chunks/embed` automatically; otherwise show "saved — will
be searchable after the next embed".

## CORS
The gateway only answers browsers whose origin is listed in `ALLOWED_ORIGINS`
(exact match, no trailing slash). If a call works from `curl` but fails in the
browser with a CORS error, the origin is missing there. Vercel preview URLs
are different origins from the production URL.
