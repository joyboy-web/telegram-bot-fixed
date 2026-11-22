"""
PayLock central config (reference module).

Reads the same environment variable names that app.py and main.py use.
app.py and main.py read the environment directly and do not import this
module, so it changes no runtime behavior. Local: values come from .env.
Render: values come from the service environment.
Entrypoint: gunicorn main:app
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def _env(name, default=""):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()

def _env_int(name, default=0):
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default

def _parse_admin_ids(raw):
    ids = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids

# Required - Telegram / Supabase / Paystack
BOT_TOKEN = _env("BOT_TOKEN")
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY")
PAYSTACK_SECRET_KEY = _env("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = _env("PAYSTACK_PUBLIC_KEY")

# Optional - Admins / URLs / Database
ADMIN_IDS = _parse_admin_ids(_env("ADMIN_IDS"))
DATABASE_URL = _env("DATABASE_URL")
WEBAPP_URL = _env("WEBAPP_URL")

# Optional - Paystack
PAYSTACK_BASE = _env("PAYSTACK_BASE", "https://api.paystack.co")

# Business constants (must stay identical to app.py / main.py / index.html / SQL trigger)
PLATFORM_FEE_KOBO = 20000
ESCROW_HOLD_HOURS = 48
ESCROW_HOLD_DAYS = 2
AUTO_RELEASE_INTERVAL_SECONDS = 300

REQUIRED_VARS = (
    "BOT_TOKEN",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "PAYSTACK_SECRET_KEY",
    "PAYSTACK_PUBLIC_KEY",
)

def missing_vars():
    """Return the names of required env vars that are not set."""
    return [name for name in REQUIRED_VARS if not _env(name)]
