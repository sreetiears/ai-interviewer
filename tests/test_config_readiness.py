"""配置预检与 API Key 解析。"""

import pytest

from config import (
    DUMMY_API_KEY,
    assert_runtime_ready,
    check_runtime_readiness,
    health_payload,
    is_usable_api_key,
    resolve_api_key,
)


def test_is_usable_api_key_rejects_blank_and_dummy():
    assert is_usable_api_key("sk-real-key") is True
    assert is_usable_api_key("") is False
    assert is_usable_api_key("   ") is False
    assert is_usable_api_key(DUMMY_API_KEY) is False
    assert is_usable_api_key(None) is False


def test_resolve_api_key_uses_dummy_only_when_allowed(monkeypatch):
    monkeypatch.delenv("ALLOW_DUMMY_KEYS", raising=False)
    assert resolve_api_key("sk-live") == "sk-live"
    assert resolve_api_key("") == ""
    assert resolve_api_key(DUMMY_API_KEY) == DUMMY_API_KEY

    monkeypatch.setenv("ALLOW_DUMMY_KEYS", "1")
    assert resolve_api_key("") == DUMMY_API_KEY
    assert resolve_api_key(DUMMY_API_KEY) == DUMMY_API_KEY


def test_check_runtime_readiness_reports_missing_keys():
    result = check_runtime_readiness(llm_api_key="", siliconflow_api_key="")
    assert result["status"] == "degraded"
    assert result["llm_configured"] is False
    assert result["speech_configured"] is False
    assert len(result["issues"]) == 2


def test_check_runtime_readiness_ok_when_both_present():
    result = check_runtime_readiness(
        llm_api_key="sk-llm",
        siliconflow_api_key="sk-speech",
    )
    assert result["status"] == "ok"
    assert result["llm_configured"] is True
    assert result["speech_configured"] is True
    assert result["issues"] == []


def test_check_runtime_readiness_rejects_dummy_as_configured():
    result = check_runtime_readiness(
        llm_api_key=DUMMY_API_KEY,
        siliconflow_api_key=DUMMY_API_KEY,
    )
    assert result["status"] == "degraded"
    assert result["llm_configured"] is False
    assert result["speech_configured"] is False


def test_assert_runtime_ready_raises_without_dummy_escape(monkeypatch):
    monkeypatch.delenv("ALLOW_DUMMY_KEYS", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    with pytest.raises(RuntimeError, match="请检查"):
        assert_runtime_ready()


def test_assert_runtime_ready_skipped_when_dummy_allowed(monkeypatch):
    monkeypatch.setenv("ALLOW_DUMMY_KEYS", "1")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    assert_runtime_ready()


def test_health_payload_shape(monkeypatch):
    monkeypatch.delenv("DELIVERY_METRICS", raising=False)
    payload = health_payload(default_mode="quick")
    assert payload["defaultMode"] == "quick"
    assert "deliveryMetrics" in payload
    assert "llmConfigured" in payload
    assert "speechConfigured" in payload
    assert payload["status"] in ("ok", "degraded")
    assert isinstance(payload["issues"], list)
