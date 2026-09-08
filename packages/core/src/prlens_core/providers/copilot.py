"""GitHub Copilot reviewer backed by the official Python SDK."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
import re
import sys
import tempfile
import time
from typing import Any

from prlens_core.providers.base import BaseReviewer, ProviderCallError, ProviderConfigurationError

logger = logging.getLogger(__name__)

_SDK_VERSION = "1.0.13"
_CLEANUP_TIMEOUT_SECONDS = 5.0
_PRIVATE_SDK_CALL = ContextVar("prlens_private_copilot_call", default=False)
_SDK_LOGGERS = ("copilot", "copilot.client", "copilot.session", "copilot._jsonrpc")
_REMEDIATIONS = {
    "sign_in",
    "switch_account",
    "show_account",
    "review_sandbox_policy",
    "allow_sandbox_outbound",
}
# Documented SessionErrorData examples in SDK v1.0.13, not exhaustive enums.
_ERROR_REASONS = {
    "authentication": ("Copilot reported an authentication failure; check workflow token authentication.", False),
    "authorization": ("Copilot reported an authorization failure; check account permissions and policy.", False),
    "quota": ("Copilot reported a quota or billing limit; check organization usage and billing.", False),
    "rate_limit": ("Copilot reported throttling; check the reported rate-limit code.", True),
    "context_limit": ("Copilot reported a context limit; reduce the review prompt size.", False),
    "query": ("Copilot reported a query failure; check request configuration and model availability.", False),
}
_ERROR_CODES = {
    "rate_limit": {
        "user_weekly_rate_limited",
        "user_global_rate_limited",
        "rate_limited",
        "user_model_rate_limited",
        "integration_rate_limited",
    },
    "quota": {"quota_exceeded", "session_quota_exceeded", "billing_not_configured"},
}
_PASSTHROUGH_ENV_VARS = {
    "CI",
    "GITHUB_ACTIONS",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SystemRoot",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


class CopilotRequestError(ProviderCallError):
    """A safe, classified Copilot failure for the shared retry loop."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class _PrivateSDKLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_SDK_CALL.get()


@contextmanager
def _private_sdk_logging():
    # SDK warnings can contain runtime stderr or exception tracebacks with prompt data.
    log_filter = _PrivateSDKLogFilter()
    loggers = [logging.getLogger(name) for name in _SDK_LOGGERS]
    for sdk_logger in loggers:
        sdk_logger.addFilter(log_filter)
    token = _PRIVATE_SDK_CALL.set(True)
    try:
        yield
    finally:
        _PRIVATE_SDK_CALL.reset(token)
        for sdk_logger in loggers:
            sdk_logger.removeFilter(log_filter)


def _load_sdk():
    if sys.version_info < (3, 11):
        raise ProviderConfigurationError("The Copilot provider requires Python 3.11 or later.")
    try:
        from copilot import CopilotClient, RuntimeConnection, rpc
    except ImportError as exc:
        raise ProviderConfigurationError(
            "The 'github-copilot-sdk' package is required for this provider. "
            "Install it with: pip install 'prlens[copilot]'"
        ) from exc
    return CopilotClient, RuntimeConnection, rpc.PermissionDecisionReject


class CopilotReviewer(BaseReviewer):
    """Run a tool-free Copilot session for one PRLens review prompt."""

    FAIL_CLOSED = True

    def __init__(self, github_token: str, model: str | None = None, timeout: float = 120):
        if not github_token or not github_token.strip():
            raise ProviderConfigurationError("GITHUB_TOKEN is required for the Copilot provider.")
        if model is not None and not isinstance(model, str):
            raise ProviderConfigurationError("copilot_model must be a string when configured.")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ProviderConfigurationError("copilot_timeout must be a number greater than zero.") from exc
        if timeout <= 0:
            raise ProviderConfigurationError("copilot_timeout must be greater than zero.")

        self.github_token = github_token
        self.model = model.strip() if model and model.strip() else None
        self.timeout = timeout
        self._client_type, self._runtime_connection_type, self._permission_reject_type = _load_sdk()

    def _is_retryable(self, error: Exception) -> bool:
        if isinstance(error, CopilotRequestError):
            return error.retryable
        return super()._is_retryable(error)

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        try:
            with _private_sdk_logging():
                return asyncio.run(self._call_api_async(system_prompt, user_prompt))
        except CopilotRequestError:
            raise
        except Exception as exc:
            raise self._request_error(exc, phase="setup") from exc

    async def _call_api_async(self, system_prompt: str, user_prompt: str) -> str:
        deadline = time.monotonic() + self.timeout
        client = None
        session = None
        session_error = None
        unsubscribe = None
        phase = "runtime_start"

        def observe(event):
            nonlocal session_error
            event_type = getattr(event, "type", None)
            if getattr(event_type, "value", event_type) == "session.error":
                session_error = event.data

        with tempfile.TemporaryDirectory(prefix="prlens-copilot-") as temp_dir:
            working_directory = os.path.join(temp_dir, "workspace")
            base_directory = os.path.join(temp_dir, "state")
            os.makedirs(working_directory)
            os.makedirs(base_directory)

            environment = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV_VARS}
            environment["GITHUB_TOKEN"] = self.github_token

            client = self._client_type(
                connection=self._runtime_connection_type.for_stdio(),
                mode="empty",
                working_directory=working_directory,
                base_directory=base_directory,
                env=environment,
                use_logged_in_user=False,
                builtin_plugin_directories=[],
                log_level="error",
            )

            try:
                await asyncio.wait_for(client.start(), timeout=self._remaining(deadline))
                phase = "create_session"
                session_options: dict[str, Any] = {
                    "on_event": observe,
                    "available_tools": [],
                    "tools": [],
                    "system_message": {"mode": "append", "content": system_prompt},
                    "on_permission_request": self._deny_permission,
                    "enable_config_discovery": False,
                    "skip_custom_instructions": True,
                    "enable_on_demand_instruction_discovery": False,
                    "enable_file_hooks": False,
                    "enable_host_git_operations": False,
                    "enable_session_store": False,
                    "enable_skills": False,
                    "included_builtin_skills": [],
                    "skill_directories": [],
                    "plugin_directories": [],
                    "instruction_directories": [],
                    "organization_custom_instructions": "",
                    "request_extensions": False,
                    "infinite_sessions": {"enabled": False},
                    "memory": {"enabled": False},
                    "streaming": False,
                }
                if self.model:
                    session_options["model"] = self.model

                session = await asyncio.wait_for(
                    client.create_session(**session_options),
                    timeout=self._remaining(deadline),
                )
                unsubscribe = session.on(observe)
                if session_error is not None:
                    raise self._request_error(None, phase=phase, session_error=session_error)
                phase = "send_and_wait"
                try:
                    response = await session.send_and_wait(
                        user_prompt,
                        agent_mode="interactive",
                        timeout=self._remaining(deadline),
                    )
                except TimeoutError as exc:
                    await self._abort(session)
                    raise self._request_error(exc, phase=phase, session_error=session_error) from exc

                if session_error is not None:
                    raise self._request_error(None, phase=phase, session_error=session_error)
                content = getattr(getattr(response, "data", None), "content", None)
                if not isinstance(content, str) or not content.strip():
                    raise CopilotRequestError("Copilot returned no review content.", retryable=False)
                return content.strip()
            except CopilotRequestError:
                raise
            except Exception as exc:
                raise self._request_error(exc, phase=phase, session_error=session_error) from exc
            finally:
                if unsubscribe is not None:
                    unsubscribe()
                await self._cleanup(client, session)

    def _deny_permission(self, _request, _invocation):
        return self._permission_reject_type(feedback="PRLens disables all Copilot tools.")

    @classmethod
    def _request_error(cls, error: Exception | None, *, phase: str, session_error=None) -> CopilotRequestError:
        status = cls._http_status(getattr(session_error, "status_code", None))
        if status is None and error is not None:
            status = cls._find_status_code(error)
        message = getattr(session_error, "message", "")
        if not isinstance(message, str):
            message = ""
        if status is None:
            status = cls._status_from_message(message)
        messages = [message] + ([str(item)[:16384] for item in cls._error_chain(error)] if error is not None else [])
        error_type = getattr(session_error, "error_type", None)
        if not isinstance(error_type, str) or error_type not in {*_ERROR_REASONS, "notification"}:
            error_type = "unknown"
        error_code = getattr(session_error, "error_code", None)
        if not isinstance(error_code, str) or error_code not in _ERROR_CODES.get(error_type, set()):
            error_code = "unknown"
        reason, retryable = cls._failure_reason(status, messages, error, error_type)
        remediation = getattr(session_error, "remediation", None)
        remediation = getattr(remediation, "value", remediation)
        if not isinstance(remediation, str) or remediation not in _REMEDIATIONS:
            remediation = "none_or_unknown"
        if status is None and remediation != "none_or_unknown":
            retryable = False
        details = [
            f"phase={phase}",
            f"source={'session.error' if session_error is not None else 'exception'}",
            f"status={status if status is not None else 'unknown'}",
            f"error_type={error_type}",
            f"error_code={error_code}",
            f"remediation={remediation}",
            f"sdk_expected={_SDK_VERSION}",
        ]
        for item in cls._error_chain(error):
            rpc_code = getattr(item, "code", None)
            if type(rpc_code) is int and -32768 <= rpc_code <= -32000:
                details.append(f"rpc_code={rpc_code}")
                if status is None and rpc_code in (-32600, -32601, -32602):
                    reason = (
                        "The runtime rejected the RPC request; check SDK/runtime compatibility and session options."
                    )
                    retryable = False
                break
        return CopilotRequestError(
            f"Copilot request failed ({'; '.join(details)}). {reason}",
            retryable=retryable,
            status_code=status,
        )

    @staticmethod
    def _failure_reason(
        status: int | None, messages: list[str], error: Exception | None, error_type: str
    ) -> tuple[str, bool]:
        if error_type in ("quota", "context_limit"):
            return _ERROR_REASONS[error_type]
        if status == 401:
            return "Authentication was rejected; check the workflow token authentication path.", False
        if status == 403:
            return (
                "Access was denied; check this run's token permissions, account/organization policy and model access. "
                "HTTP 403 alone does not identify which check failed.",
                False,
            )
        if status == 429:
            return "Copilot reported throttling or a usage limit.", True
        if status is not None and status >= 500:
            return "Copilot reported a server-side error.", True
        if status is not None and 400 <= status < 500:
            return "Copilot rejected the request; check the request configuration and model availability.", False
        if error_type in _ERROR_REASONS:
            return _ERROR_REASONS[error_type]
        # Only emit application-authored text, never arbitrary SDK messages or snippets.
        text = "\n".join(message[:16384] for message in messages).lower()
        if re.search(r"\b(?:unauthorized|authentication failed|failed to authenticate|bad credentials)\b", text):
            return "The SDK reported an authentication failure; check workflow token authentication.", False
        if re.search(r"\b(?:forbidden|permission denied|access denied)\b", text):
            return "The SDK reported an access denial; check token permissions, policy and model access.", False
        if re.search(r"\b(?:unknown model|unsupported model|model not found|model is not available)\b", text):
            return "The SDK reported an unavailable model; check model availability for this account.", False
        if re.search(r"\b(?:rate limit|rate limited|too many requests)\b", text):
            return "The SDK reported throttling or a usage limit.", True
        if isinstance(error, TimeoutError) or re.search(r"\b(?:timed out|timeout)\b", text):
            return "Copilot request timed out.", True
        if isinstance(error, (ConnectionError, OSError)):
            return "The runtime could not be reached or started; check runtime installation and network access.", True
        return "No recognized failure reason was supplied; private SDK error text was omitted.", True

    @classmethod
    def _find_status_code(cls, error: Exception) -> int | None:
        errors = list(cls._error_chain(error))
        for item in errors:
            status_code = cls._http_status(getattr(item, "status_code", None))
            if status_code is None:
                status_code = cls._find_status_code_in_data(getattr(item, "data", None))
            if status_code is not None:
                return status_code
        for item in errors:
            status_code = cls._status_from_message(str(item))
            if status_code is not None:
                return status_code
        return None

    @staticmethod
    def _error_chain(error: Exception | None):
        seen = set()
        while error is not None and id(error) not in seen and len(seen) < 8:
            seen.add(id(error))
            yield error
            error = error.__cause__ or error.__context__

    @staticmethod
    def _http_status(value) -> int | None:
        if isinstance(value, str) and re.fullmatch(r"[1-5][0-9]{2}", value):
            return int(value)
        if type(value) in (int, float) and 100 <= value <= 599 and int(value) == value:
            return int(value)
        return None

    @staticmethod
    def _status_from_message(message: str) -> int | None:
        match = re.search(
            r'\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|status(?:[_ ]?code)?["\']?\s*[:=]?\s*)'
            r"([45][0-9]{2})\b|\b(400|401|403|404|429)\s+"
            r"(?:bad request|unauthorized|forbidden|not found|too many requests)\b",
            message[:16384],
            re.IGNORECASE,
        )
        return int(match.group(1) or match.group(2)) if match else None

    @classmethod
    def _find_status_code_in_data(cls, value, depth: int = 0) -> int | None:
        if depth > 5:
            return None
        if isinstance(value, dict):
            for key in ("status_code", "statusCode", "status"):
                status_code = cls._http_status(value.get(key))
                if status_code is not None:
                    return status_code
            for nested in list(value.values())[:32]:
                status_code = cls._find_status_code_in_data(nested, depth + 1)
                if status_code is not None:
                    return status_code
        elif isinstance(value, (list, tuple)):
            for nested in value[:32]:
                status_code = cls._find_status_code_in_data(nested, depth + 1)
                if status_code is not None:
                    return status_code
        return None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Copilot request exceeded its deadline.")
        return remaining

    @classmethod
    async def _abort(cls, session) -> None:
        try:
            await asyncio.wait_for(session.abort(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Could not abort the timed-out Copilot session: %s", cls._request_error(exc, phase="abort"))

    @classmethod
    async def _cleanup(cls, client, session) -> None:
        if session is not None:
            try:
                await asyncio.wait_for(session.disconnect(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.warning(
                    "Could not disconnect the Copilot session cleanly: %s", cls._request_error(exc, phase="disconnect")
                )
        if client is None:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Could not stop the Copilot runtime cleanly: %s", cls._request_error(exc, phase="stop"))
            try:
                await asyncio.wait_for(client.force_stop(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except Exception as force_exc:
                logger.error(
                    "Could not force-stop the Copilot runtime: %s", cls._request_error(force_exc, phase="force_stop")
                )
