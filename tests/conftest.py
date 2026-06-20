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
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")

# database.py builds a real Supabase client at import time, which validates the
# key format and would reject our dummy. Replace create_client with a no-op stub
# BEFORE any app module imports it (`from supabase import create_client` then
# resolves to this stub). The tests never touch the DB, so the client is unused.
import supabase  # noqa: E402


class _StubSupabase:
    def table(self, *_a, **_k):
        raise RuntimeError("tests must not touch the database")


supabase.create_client = lambda *_a, **_k: _StubSupabase()

