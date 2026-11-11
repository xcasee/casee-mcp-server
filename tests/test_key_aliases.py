"""Tests for the CASEE_IS_API_KEY rename and legacy CASEE_API_KEY aliasing.

``apply_key_aliases()`` mirrors the new name onto ``CASEE_API_KEY`` so the
``casee`` SDK (which reads ``CASEE_API_KEY`` directly) sees the same key.
"""
import os

from casee_mcp_server.key_aliases import apply_key_aliases, resolve_is_api_key


class TestResolveIsApiKey:
    def test_new_name_wins(self, monkeypatch):
        monkeypatch.setenv("CASEE_IS_API_KEY", "casee_new")
        monkeypatch.setenv("CASEE_API_KEY", "casee_old")
        assert resolve_is_api_key() == "casee_new"

    def test_legacy_fallback(self, monkeypatch):
        monkeypatch.delenv("CASEE_IS_API_KEY", raising=False)
        monkeypatch.setenv("CASEE_API_KEY", "casee_old")
        assert resolve_is_api_key() == "casee_old"

    def test_blank_new_name_falls_back(self, monkeypatch):
        monkeypatch.setenv("CASEE_IS_API_KEY", "   ")
        monkeypatch.setenv("CASEE_API_KEY", "casee_old")
        assert resolve_is_api_key() == "casee_old"

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("CASEE_IS_API_KEY", raising=False)
        monkeypatch.delenv("CASEE_API_KEY", raising=False)
        assert resolve_is_api_key() == ""


class TestApplyKeyAliases:
    def test_mirrors_new_name_for_sdk(self, monkeypatch):
        monkeypatch.setenv("CASEE_IS_API_KEY", "casee_new")
        monkeypatch.setenv("CASEE_API_KEY", "casee_old")
        apply_key_aliases()
        assert os.environ["CASEE_API_KEY"] == "casee_new"

    def test_noop_without_new_name(self, monkeypatch):
        monkeypatch.delenv("CASEE_IS_API_KEY", raising=False)
        monkeypatch.setenv("CASEE_API_KEY", "casee_old")
        apply_key_aliases()
        assert os.environ["CASEE_API_KEY"] == "casee_old"
