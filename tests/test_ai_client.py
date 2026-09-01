"""Backend selection and model-id translation.

No client is constructed: get_client() is lazy, so these exercise the pure
decisions around it.
"""
import importlib

import ai_client


def _reload(monkeypatch, vertex_project="", api_key="x"):
    """Re-import ai_client with different settings.

    USE_VERTEX is computed at import time, so the module has to be reloaded for
    a different backend to take effect.
    """
    monkeypatch.setattr(ai_client.settings, "VERTEX_PROJECT_ID", vertex_project)
    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", api_key)
    return importlib.reload(ai_client)


class TestBackendSelection:
    def test_vertex_when_project_is_set(self, monkeypatch):
        mod = _reload(monkeypatch, vertex_project="some-project")
        assert mod.USE_VERTEX is True

    def test_direct_api_when_project_is_empty(self, monkeypatch):
        mod = _reload(monkeypatch, vertex_project="")
        assert mod.USE_VERTEX is False

    def test_missing_credentials_raise_rather_than_construct(self, monkeypatch):
        mod = _reload(monkeypatch, vertex_project="", api_key="")
        try:
            mod.get_client()
        except RuntimeError as e:
            assert "VERTEX_PROJECT_ID" in str(e) and "ANTHROPIC_API_KEY" in str(e)
        else:
            raise AssertionError("expected RuntimeError for missing credentials")


class TestModelId:
    def test_vertex_keeps_the_at_suffix(self, monkeypatch):
        mod = _reload(monkeypatch, vertex_project="some-project")
        assert mod.model_id("claude-haiku-4-5@20251001") == "claude-haiku-4-5@20251001"

    def test_direct_api_strips_the_at_suffix(self, monkeypatch):
        # The direct API has no dated-snapshot ids; sending the Vertex spelling
        # is a 404.
        mod = _reload(monkeypatch, vertex_project="")
        assert mod.model_id("claude-haiku-4-5@20251001") == "claude-haiku-4-5"

    def test_unsuffixed_ids_pass_through_on_both(self, monkeypatch):
        for project in ("some-project", ""):
            mod = _reload(monkeypatch, vertex_project=project)
            assert mod.model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"
