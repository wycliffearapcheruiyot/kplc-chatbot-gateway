"""
settings_catalog.py

Every environment variable in the KPLC chatbot system, grouped by the service
that reads it. The admin panel renders its "Environment" tab from this list
(via GET /settings), and PUT /settings/{service} validates against it.

`applies` says when an edit takes effect:
    live          the service picks it up within ~10 s, no restart
    next_session  used when the next Kaggle session is launched
    build         fixed at deploy/build time -- shown for reference, edited
                  in the host's dashboard (Render / Vercel / Netlify)
Variables whose `applies` is "build" are read-only in the admin panel.

`type` drives validation: string | url | int | float | bool | json | origins | window
`secret` only marks the value as sensitive (the panel shows it in full to a
logged-in admin, but styles it as a secret).
"""

import json
import re

LIVE, NEXT_SESSION, BUILD = "live", "next_session", "build"

MONGODB_URI_NOTE = (
    "How this service finds MongoDB, so it cannot be stored in MongoDB. "
    "Change it in the host's dashboard. Every service must use the same cluster."
)
KAGGLE_USER_DESC = "Kaggle account username (kaggle.com -> Account -> Create New API Token)."
KAGGLE_KEY_DESC = "Kaggle API key for that account."


def var(name, description, *, type="string", secret=False, default=None, applies=LIVE,
        required=False, placeholder=None):
    return {
        "name": name,
        "description": description,
        "type": type,
        "secret": secret,
        "default": default,
        "applies": applies,
        "editable": applies != BUILD,
        "required": required,
        "placeholder": placeholder,
    }


CATALOG = [
    {
        "id": "gateway",
        "label": "Gateway",
        "description": "The one API the chatbot and admin panel talk to (Render).",
        "vars": [
            var("MONGODB_URI", MONGODB_URI_NOTE, secret=True, applies=BUILD, required=True),
            var("SESSION_BACKEND_URL", "Public URL of the session backend (starts the Kaggle session, proxies /generate and /embed).",
                type="url", required=True, placeholder="https://kplc-kaggle-notebook-instance.onrender.com"),
            var("DATASET_BACKEND_URL", "Public URL of the dataset backend. Leave empty to skip the dataset check entirely.",
                type="url", placeholder="https://dataset-trigger-backend.onrender.com"),
            var("DATASET_TRIGGER_SECRET", "Sent as X-Webhook-Secret to the dataset backend. Must equal DATASET_TRIGGER_SECRET there.",
                secret=True),
            var("ADMIN_TOKEN", "Guards the admin endpoints (the token typed into the admin login box). "
                "Changing it here signs every other admin session out.", secret=True, required=True),
            var("ALLOWED_ORIGINS", "Frontend origins allowed to call this API, comma-separated, no trailing slash and no path. "
                "Saving requires the origin you are using right now to stay in the list.",
                type="origins", placeholder="https://your-chatbot.vercel.app,https://your-admin.netlify.app"),
            var("UPSTREAM_TIMEOUT_SECONDS", "Timeout for calls to the session and dataset backends. Free-tier services take 30-60 s to wake.",
                type="int", default="90"),
            var("GENERATE_TIMEOUT_SECONDS", "Timeout for a model answer (covers a cold start plus generation).",
                type="int", default="150"),
            var("EMBED_TIMEOUT_SECONDS", "Timeout for embedding requests.", type="int", default="150"),
            var("DATASET_PENDING_MAX_MINUTES", "Give up on a dataset build after this long (the dataset backend's own limit is 90).",
                type="int", default="100"),
            var("PYTHON_VERSION", "Pins the Python version Render builds with.", applies=BUILD, default="3.12.7"),
        ],
    },
    {
        "id": "session-backend",
        "label": "Session backend (inference)",
        "description": "Starts the Kaggle GPU session and proxies the model (Render). Needs MONGODB_URI to read these settings.",
        "vars": [
            var("MONGODB_URI", MONGODB_URI_NOTE + " The session backend needs it only to read these settings.",
                secret=True, applies=BUILD, required=True),
            var("KAGGLE_USERNAME", KAGGLE_USER_DESC + " Ignored when KAGGLE_ACCOUNTS is set."),
            var("KAGGLE_KEY", KAGGLE_KEY_DESC + " Ignored when KAGGLE_ACCOUNTS is set.", secret=True),
            var("KAGGLE_ACCOUNTS", 'JSON of several Kaggle accounts used in turn, e.g. {"user1":"key1","user2":"key2"}. '
                "Replaces KAGGLE_USERNAME / KAGGLE_KEY when set.", type="json", secret=True),
            var("SESSIONS_PER_ACCOUNT", "Launches one account serves in a row (3 x 8 h = 24 h) before the next takes over.",
                type="int", default="3"),
            var("ACCOUNT_SKIP_AFTER_FAILURES", "Failed launches in a row before the rotation moves to the next account.",
                type="int", default="3"),
            var("KAGGLE_KERNEL_PATH", "Kernel folder to push, relative to where main.py runs.", default="./kplc-kaggle-notebook"),
            var("SESSION_WEBHOOK_SECRET", "Shared secret serve.py sends on its webhook calls. A session that is already running "
                "keeps the old value until it restarts, so its webhooks are rejected until then.", secret=True, required=True),
            var("CLOUDFLARE_TUNNEL_TOKEN", "Cloudflare Tunnel token. Pushed to Kaggle with each new session.",
                secret=True, required=True, applies=NEXT_SESSION),
            var("MODEL_TUNNEL_URL", "Fixed Cloudflare Tunnel hostname that reaches the model.", type="url", required=True,
                placeholder="https://model.yourdomain.com"),
            var("BACKEND_URL", "This service's own public URL, handed to serve.py for its callbacks. "
                "Leave empty on Render (RENDER_EXTERNAL_URL is used).", type="url", applies=NEXT_SESSION),
            var("SESSION_START_TIMEOUT_SECONDS", "How long to wait for /session/ready before giving up.", type="int", default="600"),
            var("PROXY_TIMEOUT_SECONDS", "Timeout for proxied /generate and /embed calls.", type="int", default="120"),
            var("AUTO_KEEP_ALIVE", "true keeps a Kaggle session running at all times; false starts on demand.",
                type="bool", default="false"),
            var("RESTART_EVERY_HOURS", "How long a Kaggle session runs before its scheduled restart (values above 11.5 are clamped).",
                type="float", default="8", applies=NEXT_SESSION),
            var("KAGGLE_IDLE_TIMEOUT_SECONDS", "Idle shutdown inside serve.py in seconds; 0 = never. "
                "Default is 0 when AUTO_KEEP_ALIVE is true, otherwise 900.", type="int", applies=NEXT_SESSION),
            var("KEEP_ALIVE_WINDOW_UTC", "Only launch sessions inside this UTC window, e.g. 05:00-13:00. Empty = always.",
                type="window", placeholder="05:00-13:00"),
            var("SUPERVISOR_INTERVAL_SECONDS", "How often the always-on supervisor checks the session.", type="int", default="60"),
            var("RESTART_RETRY_BACKOFF_SECONDS", "Wait before relaunching after a failed launch.", type="int", default="300"),
            var("PYTHON_VERSION", "Pins the Python version Render builds with.", applies=BUILD, default="3.12.7"),
        ],
    },
    {
        "id": "dataset-backend",
        "label": "Dataset backend (dataset-sync)",
        "description": "Keeps the model dataset on Kaggle, copying it from Hugging Face when missing (Render).",
        "vars": [
            var("MONGODB_URI", MONGODB_URI_NOTE, secret=True, applies=BUILD, required=True),
            var("MONGODB_DB", "Database this service keeps its own state in. Fixed at deploy time.", applies=BUILD, default="dataset_backend"),
            var("DATASET_TRIGGER_SECRET", "Callers send it as the X-Webhook-Secret header.", secret=True, required=True),
            var("KAGGLE_USERNAME", KAGGLE_USER_DESC, required=True),
            var("KAGGLE_KEY", KAGGLE_KEY_DESC + " Or set KAGGLE_API_TOKEN instead.", secret=True),
            var("KAGGLE_API_TOKEN", "New-style Kaggle token (alternative to KAGGLE_KEY).", secret=True),
            var("KAGGLE_DATASET", "owner/slug of the Kaggle dataset that must exist.", required=True,
                placeholder="your-kaggle-username/your-dataset-slug"),
            var("HF_REPO_ID", "Hugging Face repo copied into that dataset when it is missing.", required=True,
                placeholder="org/repo-name"),
            var("HF_REVISION", "Branch, tag or commit of the Hugging Face repo.", placeholder="main"),
            var("HF_TOKEN", "Only needed if the Hugging Face repo is private or gated.", secret=True),
            var("DATASET_TITLE", "Title for the Kaggle dataset (defaults to the slug)."),
            var("DATASET_EXPECTED_FILE", "File that must be present in the dataset, e.g. model.safetensors."),
            var("DATASET_KERNEL_SLUG", "Slug of the Kaggle kernel that does the copy.", default="hf-to-kaggle-dataset"),
            var("DATASET_SECRETS_SLUG", "Private Kaggle dataset used to hand the Kaggle key / HF token to the kernel.",
                default="dataset-trigger-secrets"),
            var("DATASET_PREPARE_TIMEOUT_MINUTES", "Give up on a dataset build after this long.", type="int", default="90"),
            var("PYTHON_VERSION", "Pins the Python version Render builds with.", applies=BUILD, default="3.11.9"),
        ],
    },
    {
        "id": "kb-builder",
        "label": "KB builder",
        "description": "Knowledge-base tooling. Shares the session and dataset code of the two backends above.",
        "vars": [
            var("MONGODB_URI", MONGODB_URI_NOTE, secret=True, applies=BUILD, required=True),
            var("KAGGLE_USERNAME", KAGGLE_USER_DESC, required=True),
            var("KAGGLE_KEY", KAGGLE_KEY_DESC, secret=True, required=True),
            var("KAGGLE_KERNEL_REPO", "GitHub repo holding the Kaggle notebook (owner/repo).", placeholder="your-github-username/kplc-kaggle-notebook"),
            var("KAGGLE_KERNEL_REF", "Branch or tag of that repo.", default="main"),
            var("GITHUB_TOKEN", "Read-only token, only needed if that repo is private.", secret=True),
            var("KAGGLE_KERNEL_DIR", "Local development only: push a folder on disk instead of GitHub. Leave empty on Render."),
            var("STARTING_TIMEOUT_MINUTES", "A session stuck in 'starting' this long is treated as idle.", type="int", default="15"),
            var("READY_MAX_AGE_HOURS", "A session 'ready' this long is treated as idle.", type="int", default="12"),
            var("MODEL_TUNNEL_URL", "Fixed Cloudflare Tunnel hostname.", type="url"),
            var("SESSION_WEBHOOK_SECRET", "Shared secret for the /session/ready and /session/ended webhooks.", secret=True),
            var("ALLOWED_ORIGINS", "Frontend origins allowed to call this API, comma-separated.", type="origins"),
            var("KAGGLE_DATASET", "owner/slug of the Kaggle dataset that must exist.", placeholder="owner/slug"),
            var("HF_REPO_ID", "Hugging Face repo copied into that dataset when it is missing."),
            var("DATASET_TRIGGER_SECRET", "Callers send it as the X-Webhook-Secret header.", secret=True),
            var("HF_REVISION", "Branch, tag or commit of the Hugging Face repo."),
            var("DATASET_TITLE", "Title for the Kaggle dataset (defaults to the slug)."),
            var("DATASET_EXPECTED_FILE", "File that must be present in the dataset."),
            var("DATASET_KERNEL_SLUG", "Slug of the Kaggle kernel that does the copy.", default="hf-to-kaggle-dataset"),
            var("DATASET_PREPARE_TIMEOUT_MINUTES", "Give up on a dataset build after this long.", type="int", default="90"),
        ],
    },
    {
        "id": "db-infra",
        "label": "DB infra (populate_atlas.py)",
        "description": "One-off script that seeds MongoDB Atlas. Runs on your machine, so it reads its own .env.",
        "vars": [
            var("MONGODB_URI", "Atlas connection string used by populate_atlas.py.", secret=True, applies=BUILD, required=True),
        ],
    },
    {
        "id": "chatbot-web",
        "label": "Chatbot frontend (Vercel)",
        "description": "Next.js app. NEXT_PUBLIC_ values are baked into the browser bundle at build time, "
                       "so they are edited in Vercel and need a redeploy.",
        "vars": [
            var("NEXT_PUBLIC_API_URL", "Gateway's public URL. The one service the chatbot calls.", type="url", applies=BUILD, required=True),
            var("NEXT_PUBLIC_SESSION_BACKEND_URL", "Session backend URL, pinged at /health to wake it. Blank skips warming.", type="url", applies=BUILD),
            var("NEXT_PUBLIC_DATASET_BACKEND_URL", "Dataset backend URL, pinged at /health to wake it. Blank skips warming.", type="url", applies=BUILD),
        ],
    },
    {
        "id": "admin",
        "label": "Admin panel (Netlify)",
        "description": "This app. VITE_ values are copied into the public JavaScript at build time (never put secrets here). "
                       "The values below are the ones this deployment was built with.",
        "vars": [
            var("VITE_API_URL", "Gateway's public URL. Needed to reach the gateway at all, so it cannot be stored in it.",
                type="url", applies=BUILD, required=True),
            var("VITE_SESSION_BACKEND_URL", "Session backend URL, pinged at /health to wake it. Blank skips warming.", type="url", applies=BUILD),
            var("VITE_DATASET_BACKEND_URL", "Dataset backend URL, pinged at /health to wake it. Blank skips warming.", type="url", applies=BUILD),
        ],
    },
]

SERVICES = {s["id"]: s for s in CATALOG}


def names_for(service_id: str) -> list[str]:
    return [v["name"] for v in SERVICES[service_id]["vars"]]


def find_var(service_id: str, name: str):
    return next((v for v in SERVICES[service_id]["vars"] if v["name"] == name), None)


# --- validation ----------------------------------------------------------------

_ORIGIN = re.compile(r"^https?://[^/\s?#]+$")
_WINDOW = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")
_BOOLS = {"1", "0", "true", "false", "yes", "no", "on", "off"}


def normalize_origins(raw: str) -> list[str]:
    return [o.strip().rstrip("/") for o in (raw or "").split(",") if o.strip()]


def validate(spec: dict, value: str) -> str | None:
    """Returns an error message, or None if `value` is acceptable for this variable."""
    t, v = spec["type"], value.strip()
    name = spec["name"]
    if name == "ADMIN_TOKEN" and len(v) < 12:
        return "must be at least 12 characters (try: openssl rand -hex 32)."
    if t == "string" or v == "":
        return None
    if t == "url":
        return None if re.match(r"^https?://[^\s/]+\S*$", v) else "must be a full URL starting with http:// or https://."
    if t == "int":
        return None if re.fullmatch(r"\d+", v) else "must be a whole number (0 or more)."
    if t == "float":
        try:
            return None if float(v) > 0 else "must be greater than 0."
        except ValueError:
            return "must be a number."
    if t == "bool":
        return None if v.lower() in _BOOLS else "must be true or false."
    if t == "json":
        try:
            data = json.loads(v)
        except json.JSONDecodeError as e:
            return f"is not valid JSON ({e.msg})."
        return None if isinstance(data, (dict, list)) else "must be a JSON object or list."
    if t == "origins":
        bad = [o for o in normalize_origins(v) if not _ORIGIN.match(o)]
        return None if not bad else f"has an invalid origin: {bad[0]} (use https://host, no path or trailing slash)."
    if t == "window":
        if not _WINDOW.match(v):
            return "must look like HH:MM-HH:MM (UTC)."
        try:
            for part in v.split("-"):
                h, m = (int(x) for x in part.split(":"))
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError
        except ValueError:
            return "has an hour or minute out of range."
        return None
    return None
