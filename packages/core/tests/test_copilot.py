"""Tests for the restricted GitHub Copilot provider."""

from __future__ import annotations

import json
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
    def __init__(self, content="[]", error=None):
        self.content = content
        self.error = error
        self.send_count = 0
        self.aborted = False
        self.disconnected = False

    async def send_and_wait(self, prompt, **kwargs):
        self.send_count += 1
        self.prompt = prompt
        self.send_options = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(data=SimpleNamespace(content=self.content))

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.disconnected = True


class _Client:
    instances = []
    session_factory = staticmethod(_Session)
    start_error = None

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
