"""Test configuration.

Application modules build their config at import time via `Settings()`, whose
fields are required. Populate the environment with harmless dummy values BEFORE
any app module is imported so the suite runs with no real credentials — this is
what lets CI run on a fresh checkout.

Unlike the previous Supabase setup, no client stubbing is needed: the Cloud SQL
engine is built lazily inside get_engine(), and the Vertex client resolves
credentials only when a request is actually made. None of these tests make
network calls.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000000")
os.environ.setdefault("WHATSAPP_BUSINESS_ACCOUNT_ID", "000000000000000")
os.environ.setdefault("WEBHOOK_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("INSTANCE_CONNECTION_NAME", "test-project:asia-southeast1:test")
os.environ.setdefault("DB_NAME", "sitebot")
os.environ.setdefault("DB_USER", "test-user")
os.environ.setdefault("VERTEX_PROJECT_ID", "test-project")
