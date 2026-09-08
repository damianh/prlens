"""GitHub Copilot reviewer backed by the official Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from typing import Any

from prlens_core.providers.base import BaseReviewer, ProviderConfigurationError

logger = logging.getLogger(__name__)

_SDK_VERSION = "1.0.13"
_CLEANUP_TIMEOUT_SECONDS = 5.0
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


class CopilotRequestError(RuntimeError):
    """A safe, classified Copilot failure for the shared retry loop."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


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
            return asyncio.run(self._call_api_async(system_prompt, user_prompt))
        except CopilotRequestError:
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise CopilotRequestError("Copilot request timed out or lost its connection.", retryable=True) from exc
        except Exception as exc:
            status_code = self._find_status_code(exc)
            retryable = status_code is None or status_code == 429 or status_code >= 500
            raise CopilotRequestError("Copilot request failed.", retryable=retryable) from exc

    async def _call_api_async(self, system_prompt: str, user_prompt: str) -> str:
        deadline = time.monotonic() + self.timeout
        client = None
        session = None

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
                session_options: dict[str, Any] = {
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
                try:
                    response = await session.send_and_wait(
                        user_prompt,
                        agent_mode="interactive",
                        timeout=self._remaining(deadline),
                    )
                except TimeoutError as exc:
                    await self._abort(session)
                    raise CopilotRequestError("Copilot request timed out.", retryable=True) from exc

                content = getattr(getattr(response, "data", None), "content", None)
                if not isinstance(content, str) or not content.strip():
                    raise CopilotRequestError("Copilot returned no review content.", retryable=False)
                return content.strip()
            finally:
                await self._cleanup(client, session)

    def _deny_permission(self, _request, _invocation):
        return self._permission_reject_type(feedback="PRLens disables all Copilot tools.")

    @classmethod
    def _find_status_code(cls, error: Exception) -> int | None:
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        data = getattr(error, "data", None)
        return cls._find_status_code_in_data(data)

    @classmethod
    def _find_status_code_in_data(cls, value) -> int | None:
        if isinstance(value, dict):
            for key in ("status_code", "statusCode", "status"):
                status_code = value.get(key)
                if isinstance(status_code, int):
                    return status_code
            for nested in value.values():
                status_code = cls._find_status_code_in_data(nested)
                if status_code is not None:
                    return status_code
        elif isinstance(value, (list, tuple)):
            for nested in value:
                status_code = cls._find_status_code_in_data(nested)
                if status_code is not None:
                    return status_code
        return None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Copilot request exceeded its deadline.")
        return remaining

    @staticmethod
    async def _abort(session) -> None:
        try:
            await asyncio.wait_for(session.abort(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Could not abort the timed-out Copilot session: %s", exc)

    @staticmethod
    async def _cleanup(client, session) -> None:
        if session is not None:
            try:
                await asyncio.wait_for(session.disconnect(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.warning("Could not disconnect the Copilot session cleanly: %s", exc)
        if client is None:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Could not stop the Copilot runtime cleanly: %s", exc)
            try:
                await asyncio.wait_for(client.force_stop(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except Exception as force_exc:
                logger.error("Could not force-stop the Copilot runtime: %s", force_exc)
