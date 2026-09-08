"""Tests for the restricted GitHub Copilot provider."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from prlens_core.providers.base import ProviderCallError, ProviderConfigurationError, ProviderResponseError
from prlens_core.providers.copilot import CopilotRequestError, CopilotReviewer, _load_sdk


class _PermissionReject:
    def __init__(self, feedback=None):
        self.feedback = feedback


class _RuntimeConnection:
    @staticmethod
    def for_stdio():
        return "managed-stdio"


class _Session:
    def __init__(self, content="[]", error=None, error_data=None):
        self.content = content
        self.error = error
        self.error_data = error_data
        self.handlers = set()
        self.send_count = 0
        self.aborted = False
        self.disconnected = False

    async def send_and_wait(self, prompt, **kwargs):
        self.send_count += 1
        self.prompt = prompt
        self.send_options = kwargs
        if self.error_data is not None:
            self.emit_error()
        if self.error:
            raise self.error
        return SimpleNamespace(data=SimpleNamespace(content=self.content))

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.disconnected = True
        self.handlers.clear()

    def on(self, handler):
        self.handlers.add(handler)
        return lambda: self.handlers.discard(handler)

    def emit_error(self):
        for handler in list(self.handlers):
            handler(SimpleNamespace(type=SimpleNamespace(value="session.error"), data=self.error_data))


class _Client:
    instances = []
    session_factory = staticmethod(_Session)
    start_error = None
    create_error = None

    def __init__(self, **kwargs):
        self.options = kwargs
        self.session = self.session_factory()
        self.session_options = None
        self.stopped = False
        self.force_stopped = False
        self.__class__.instances.append(self)

    async def start(self):
        if self.start_error:
            raise self.start_error

    async def create_session(self, **kwargs):
        self.session_options = kwargs
        self.session.on(kwargs["on_event"])
        if self.create_error:
            self.session.emit_error()
            raise self.create_error
        return self.session

    async def stop(self):
        self.stopped = True

    async def force_stop(self):
        self.force_stopped = True


@pytest.fixture(autouse=True)
def _reset_client():
    _Client.instances = []
    _Client.session_factory = staticmethod(_Session)
    _Client.start_error = None
    _Client.create_error = None


def _reviewer(mocker, **kwargs):
    mocker.patch(
        "prlens_core.providers.copilot._load_sdk",
        return_value=(_Client, _RuntimeConnection, _PermissionReject),
    )
    return CopilotReviewer("workflow-token", **kwargs)


def test_requires_token(mocker):
    mocker.patch(
        "prlens_core.providers.copilot._load_sdk",
        return_value=(_Client, _RuntimeConnection, _PermissionReject),
    )
    with pytest.raises(ProviderConfigurationError, match="GITHUB_TOKEN"):
        CopilotReviewer("")


def test_requires_python_311(monkeypatch):
    monkeypatch.setattr("prlens_core.providers.copilot.sys.version_info", (3, 10))
    with pytest.raises(ProviderConfigurationError, match="Python 3.11"):
        _load_sdk()


def test_configures_tool_free_isolated_session(mocker, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "wrong-token")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "wrong-copilot-token")
    monkeypatch.setenv("COPILOT_MODEL", "ambient-model")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-child")
    reviewer = _reviewer(mocker)

    assert reviewer._call_api("reviewer rules", "review this") == "[]"

    client = _Client.instances[0]
    assert client.options["mode"] == "empty"
    assert client.options["connection"] == "managed-stdio"
    assert client.options["use_logged_in_user"] is False
    assert client.options["builtin_plugin_directories"] == []
    assert client.options["env"]["GITHUB_TOKEN"] == "workflow-token"
    assert "github_token" not in client.options
    assert "GH_TOKEN" not in client.options["env"]
    assert "COPILOT_GITHUB_TOKEN" not in client.options["env"]
    assert "COPILOT_MODEL" not in client.options["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in client.options["env"]

    options = client.session_options
    assert options["available_tools"] == []
    assert options["tools"] == []
    assert options["system_message"] == {"mode": "append", "content": "reviewer rules"}
    assert options["enable_config_discovery"] is False
    assert options["skip_custom_instructions"] is True
    assert options["enable_on_demand_instruction_discovery"] is False
    assert options["enable_file_hooks"] is False
    assert options["enable_host_git_operations"] is False
    assert options["enable_session_store"] is False
    assert options["enable_skills"] is False
    assert options["included_builtin_skills"] == []
    assert options["skill_directories"] == []
    assert options["plugin_directories"] == []
    assert options["instruction_directories"] == []
    assert options["request_extensions"] is False
    assert options["memory"] == {"enabled": False}
    assert "model" not in options
    assert client.session.send_options["agent_mode"] == "interactive"
    assert client.session.disconnected is True
    assert client.stopped is True


def test_passes_optional_model(mocker):
    reviewer = _reviewer(mocker, model="gpt-5")

    reviewer._call_api("rules", "prompt")

    assert _Client.instances[0].session_options["model"] == "gpt-5"


def test_denies_any_unexpected_permission_request(mocker):
    reviewer = _reviewer(mocker)

    decision = reviewer._deny_permission(object(), {})

    assert isinstance(decision, _PermissionReject)
    assert "disables all" in decision.feedback


def test_validates_structured_review_response(mocker):
    payload = [{"line": 4, "severity": "major", "comment": "Handle the error."}]
    _Client.session_factory = staticmethod(lambda: _Session(content=json.dumps(payload)))
    reviewer = _reviewer(mocker)

    result = reviewer.review("desc", "file.py", "+bad()", "bad()", "rules")

    assert result == payload


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{}",
        '[{"line": true, "severity": "major", "comment": "bad"}]',
        '[{"line": 1, "severity": "unknown", "comment": "bad"}]',
        '[{"line": 1, "severity": "major", "comment": ""}]',
    ],
)
def test_invalid_responses_fail_closed(mocker, content):
    _Client.session_factory = staticmethod(lambda: _Session(content=content))
    reviewer = _reviewer(mocker)

    expected = ProviderCallError if not content else ProviderResponseError
    with pytest.raises(expected):
        reviewer.review("desc", "file.py", "+bad()", "bad()", "rules")


@pytest.mark.parametrize("content", ["Private source code", '{"comment": "Private source code"}'])
def test_invalid_response_is_not_logged(mocker, caplog, content):
    _Client.session_factory = staticmethod(lambda: _Session(content=content))
    reviewer = _reviewer(mocker)

    with pytest.raises(ProviderResponseError) as raised:
        reviewer.review("desc", "file.py", "+bad()", "bad()", "rules")

    assert "Private source code" not in caplog.text + str(raised.value)
    assert "content omitted" in caplog.text


def test_timeout_aborts_and_cleans_up(mocker):
    _Client.session_factory = staticmethod(lambda: _Session(error=TimeoutError()))
    reviewer = _reviewer(mocker)

    with pytest.raises(CopilotRequestError, match="timed out"):
        reviewer._call_api("rules", "prompt")

    client = _Client.instances[0]
    assert client.session.aborted is True
    assert client.session.disconnected is True
    assert client.stopped is True


def test_http_403_is_not_retried(mocker):
    class _ForbiddenError(Exception):
        data = {"response": {"statusCode": 403}}

    _Client.start_error = _ForbiddenError()
    reviewer = _reviewer(mocker)
    mock_sleep = mocker.patch("prlens_core.providers.base.time.sleep")

    with pytest.raises(ProviderCallError):
        reviewer.review("desc", "file.py", "+bad()", "bad()", "rules")

    assert len(_Client.instances) == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    "status,remediation,attempts",
    [(401, None, 1), (403, None, 1), (422, None, 1), (429, None, 3), (503, None, 3), (None, "sign_in", 1)],
)
def test_structured_session_error_survives_sdk_exception(mocker, caplog, status, remediation, attempts):
    private = "workflow-token PRIVATE_SOURCE private_file.py"
    data = SimpleNamespace(
        status_code=status,
        remediation=SimpleNamespace(value=remediation),
        error_type=private,
        error_code=private,
        message=private,
        stack=private,
        url=f"https://private.example/{private}",
        service_request_id=private,
        provider_call_id=private,
    )
    _Client.session_factory = staticmethod(
        lambda: _Session(error=Exception(f"Session error: {private}"), error_data=data)
    )
    reviewer = _reviewer(mocker)
    sleep = mocker.patch("prlens_core.providers.base.time.sleep")

    with pytest.raises(ProviderCallError) as raised:
        reviewer.review("PRIVATE_DESCRIPTION", "private_file.py", "+PRIVATE_SOURCE", "PRIVATE_SOURCE", "PRIVATE_RULES")

    diagnostic = str(raised.value)
    assert "phase=send_and_wait" in diagnostic
    assert "source=session.error" in diagnostic
    assert f"status={status if status else 'unknown'}" in diagnostic
    assert f"remediation={remediation or 'none_or_unknown'}" in diagnostic
    assert raised.value.__cause__.status_code == status
    assert len(_Client.instances) == attempts
    assert sleep.call_count == attempts - 1
    for value in private.split() + ["PRIVATE_DESCRIPTION", "PRIVATE_RULES", "private.example"]:
        assert value not in diagnostic + caplog.text
    assert len(diagnostic) < 1000
    assert all(
        client.stopped and client.session.disconnected and not client.session.handlers for client in _Client.instances
    )


def test_captures_session_error_during_creation(mocker):
    data = SimpleNamespace(status_code=403, message="Private error")
    _Client.session_factory = staticmethod(lambda: _Session(error_data=data))
    _Client.create_error = Exception("Opaque SDK exception")
    reviewer = _reviewer(mocker)

    with pytest.raises(CopilotRequestError) as raised:
        reviewer._call_api("rules", "prompt")

    assert raised.value.status_code == 403
    assert raised.value.retryable is False
    assert "phase=create_session" in str(raised.value)
    assert _Client.instances[0].session.send_count == 0
    assert _Client.instances[0].stopped


@pytest.mark.parametrize(
    "error_type,error_code,status,retryable",
    [
        ("authentication", None, None, False),
        ("authorization", None, None, False),
        ("context_limit", None, None, False),
        ("query", None, None, False),
        ("notification", None, None, True),
        ("quota", "quota_exceeded", 429, False),
        ("quota", "session_quota_exceeded", None, False),
        ("quota", "billing_not_configured", None, False),
        ("rate_limit", "user_weekly_rate_limited", 429, True),
        ("rate_limit", "user_global_rate_limited", None, True),
        ("rate_limit", "rate_limited", None, True),
        ("rate_limit", "user_model_rate_limited", None, True),
        ("rate_limit", "integration_rate_limited", None, True),
    ],
)
def test_documented_error_types_and_codes_are_preserved(error_type, error_code, status, retryable):
    data = SimpleNamespace(error_type=error_type, error_code=error_code, status_code=status, message="PRIVATE_SOURCE")

    error = CopilotReviewer._request_error(None, phase="send_and_wait", session_error=data)

    assert f"error_type={error_type}" in str(error)
    assert f"error_code={error_code or 'unknown'}" in str(error)
    assert error.retryable is retryable
    assert "PRIVATE_SOURCE" not in str(error)


def test_session_error_cannot_become_a_clean_review(mocker):
    _Client.session_factory = staticmethod(
        lambda: _Session(content="[]", error_data=SimpleNamespace(status_code=403, message="Private error"))
    )
    reviewer = _reviewer(mocker)

    with pytest.raises(CopilotRequestError, match="status=403"):
        reviewer._call_api("rules", "prompt")


@pytest.mark.parametrize(
    "message,status,retryable,reason",
    [
        ("Session error: Request failed with status code 403 private content", 403, False, "Access was denied"),
        ('Session error: {"statusCode": 429, "body": "private content"}', 429, True, "throttling"),
        ("HTTP/1.1 503 private content", 503, True, "server-side"),
        ("Session error: Authentication failed private content", None, False, "authentication failure"),
        ("Session error: unsupported model private content", None, False, "unavailable model"),
        ("Session error: 403 characters of private content", None, True, "No recognized failure"),
    ],
)
def test_legacy_exception_message_classification_is_not_raw_output(mocker, caplog, message, status, retryable, reason):
    _Client.session_factory = staticmethod(lambda: _Session(error=Exception(message)))
    reviewer = _reviewer(mocker)

    with pytest.raises(CopilotRequestError) as raised:
        reviewer._call_api("rules", "prompt")

    assert raised.value.status_code == status
    assert raised.value.retryable is retryable
    assert reason in str(raised.value)
    assert "private content" not in str(raised.value) + caplog.text


def test_nested_rpc_status_precedes_message_and_survives_wrapping(mocker):
    cause = Exception("HTTP 503 private response")
    cause.code = -32603
    cause.data = {"response": {"statusCode": "403"}}
    error = RuntimeError("Wrapper without HTTP status")
    error.__cause__ = cause
    _Client.start_error = error
    reviewer = _reviewer(mocker)

    with pytest.raises(CopilotRequestError) as raised:
        reviewer._call_api("rules", "prompt")

    assert raised.value.status_code == 403
    assert raised.value.retryable is False
    assert "phase=runtime_start" in str(raised.value)
    assert "rpc_code=-32603" in str(raised.value)
    assert "private response" not in str(raised.value)


@pytest.mark.parametrize("rpc_code,retryable", [(-32600, False), (-32601, False), (-32602, False), (-32603, True)])
def test_rpc_codes_are_not_http_statuses(rpc_code, retryable):
    original = Exception("PRIVATE_RPC_ERROR")
    original.code = rpc_code

    error = CopilotReviewer._request_error(original, phase="create_session")

    assert error.status_code is None
    assert f"rpc_code={rpc_code}" in str(error)
    assert error.retryable is retryable
    assert "PRIVATE_RPC_ERROR" not in str(error)


@pytest.mark.parametrize("invalid", [True, False, 403.5, float("inf"), 0, -32603, 999, "403 private"])
def test_does_not_misclassify_invalid_status_values(invalid):
    assert CopilotReviewer._find_status_code_in_data({"status_code": invalid}) is None


def test_status_scan_handles_cycles():
    data = {}
    data["self"] = data
    error = Exception("Unknown")
    error.data = data
    error.__cause__ = error

    assert CopilotReviewer._find_status_code(error) is None


def test_cleanup_errors_do_not_expose_private_data_or_mask_failure(mocker, caplog):
    _Client.session_factory = staticmethod(lambda: _Session(error=TimeoutError("PRIVATE_TIMEOUT")))
    reviewer = _reviewer(mocker)
    mocker.patch.object(_Session, "abort", side_effect=RuntimeError("PRIVATE_ABORT workflow-token"))
    mocker.patch.object(_Session, "disconnect", side_effect=RuntimeError("PRIVATE_DISCONNECT workflow-token"))
    mocker.patch.object(_Client, "stop", side_effect=RuntimeError("PRIVATE_STOP workflow-token"))
    force_stop = mocker.patch.object(_Client, "force_stop", side_effect=RuntimeError("PRIVATE_FORCE workflow-token"))

    with pytest.raises(CopilotRequestError, match="timed out"):
        reviewer._call_api("rules", "prompt")

    force_stop.assert_called_once()
    for phase in ("abort", "disconnect", "stop", "force_stop"):
        assert f"phase={phase}" in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert "workflow-token" not in caplog.text
    assert not _Client.instances[0].session.handlers


def test_sdk_raw_logging_is_suppressed_only_during_this_provider_call(mocker, caplog):
    from contextvars import Context

    from prlens_core.providers.copilot import _SDK_LOGGERS

    reviewer = _reviewer(mocker)
    filters_before = {name: list(logging.getLogger(name).filters) for name in _SDK_LOGGERS}

    async def noisy_failure():
        for name in _SDK_LOGGERS:
            sdk_logger = logging.getLogger(name)
            try:
                raise RuntimeError("PRIVATE_TRACE workflow-token")
            except RuntimeError:
                sdk_logger.warning("PRIVATE_STDERR", exc_info=True)
            Context().run(sdk_logger.warning, "Other caller still visible")
        logging.getLogger("another_provider").warning("Unrelated provider still visible")
        raise RuntimeError("PRIVATE_EXCEPTION")

    mocker.patch.object(_Client, "start", side_effect=noisy_failure)
    with pytest.raises(CopilotRequestError):
        reviewer._call_api("rules", "prompt")

    logging.getLogger("copilot.session").warning("Later SDK call still visible")
    assert "PRIVATE_" not in caplog.text
    assert "workflow-token" not in caplog.text
    assert "Other caller still visible" in caplog.text
    assert "Unrelated provider still visible" in caplog.text
    assert "Later SDK call still visible" in caplog.text
    assert {name: list(logging.getLogger(name).filters) for name in _SDK_LOGGERS} == filters_before
