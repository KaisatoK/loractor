"""Tests for model/tool error middleware and retry/backoff."""

import backend.pipeline.middleware as middleware
from langchain.agents.middleware import ModelResponse


def test_classify_error_kinds():
    assert middleware._classify(Exception("Rate limit exceeded")) == "rate_limit"
    assert middleware._classify(Exception("request too large")) == "too_large"
    assert middleware._classify(Exception("connection reset by peer")) == "connection"
    assert middleware._classify(Exception("the request timed out")) == "connection"
    assert middleware._classify(Exception("mysterious failure")) == "unknown"


def test_retry_after_parsing():
    assert middleware._retry_after_seconds(Exception("Please try again in 2.5s")) == 2.5
    assert middleware._retry_after_seconds(Exception("retry after 3 seconds")) == 3.0
    assert middleware._retry_after_seconds(Exception("no hint here")) is None


def test_transient_failure_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(middleware.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection reset")
        return "ok"

    wrapped = middleware.make_error_middleware()
    assert wrapped.wrap_model_call(object(), handler) == "ok"
    assert calls["n"] == 2


def test_retries_are_bounded_and_return_fallback(monkeypatch):
    monkeypatch.setattr(middleware.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise ConnectionError("connection reset")

    wrapped = middleware.make_error_middleware()
    response = wrapped.wrap_model_call(object(), handler)
    assert isinstance(response, ModelResponse)
    assert calls["n"] == middleware.MAX_MODEL_RETRIES


def test_non_retryable_error_returns_immediately(monkeypatch):
    monkeypatch.setattr(middleware.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise ValueError("request too large")

    wrapped = middleware.make_error_middleware()
    response = wrapped.wrap_model_call(object(), handler)
    assert isinstance(response, ModelResponse)
    assert calls["n"] == 1


def test_fallback_factory_receives_message(monkeypatch):
    monkeypatch.setattr(middleware.time, "sleep", lambda *_: None)

    def handler(request):
        raise ValueError("request too large")

    wrapped = middleware.make_error_middleware(
        fallback_factory=lambda msg: {"fallback": msg}
    )
    response = wrapped.wrap_model_call(object(), handler)
    assert response.structured_response == {"fallback": response.result[0].content}
