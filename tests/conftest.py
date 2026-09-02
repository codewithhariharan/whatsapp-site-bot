"""Test configuration.

The application modules build their config (and the Anthropic client) at import
time via `Settings()`, whose fields are required. Populate the environment with
harmless dummy values BEFORE any app module is imported so the suite runs with
no real secrets — this is what lets CI run on a fresh checkout without
credentials. None of these tests make network calls.
"""
import os
import sys
from pathlib import Path

# Make the project root importable (so `import commands`, `import config`, ... work
# regardless of where pytest is invoked from).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
# Required since the Meta Cloud API path was removed: the bridge is the only
# transport, so an unset value is a misconfiguration rather than a fallback.
os.environ.setdefault("BAILEYS_BRIDGE_URL", "http://bridge:8088")
os.environ.setdefault("BRIDGE_SHARED_SECRET", "test-bridge-secret")

# database.py builds its connection pool lazily, so importing it needs no
# live database. The tests never touch the DB.
