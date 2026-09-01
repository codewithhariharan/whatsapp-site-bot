"""Test configuration.

The application modules build their config (and the Supabase/Anthropic clients)
at import time via `Settings()`, whose fields are required. Populate the
environment with harmless dummy values BEFORE any app module is imported so the
suite runs with no real secrets — this is what lets CI run on a fresh checkout
without credentials. None of these tests make network calls.
"""
import os
import sys
from pathlib import Path

# Make the project root importable (so `import commands`, `import config`, ... work
# regardless of where pytest is invoked from).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000000")
os.environ.setdefault("WHATSAPP_BUSINESS_ACCOUNT_ID", "000000000000000")
os.environ.setdefault("WEBHOOK_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")

# database.py builds its connection pool lazily, so importing it needs no
# live database. The tests never touch the DB.

