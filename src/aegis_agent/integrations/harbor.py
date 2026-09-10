"""Harbor 0.22 custom installed-agent adapter for Aegis.

Harbor is intentionally an optional dependency. Import this module only from a
Harbor environment, for example with ``-a aegis_agent.integrations.harbor:Aegis``.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    with_prompt_template,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from aegis_agent import __version__
from aegis_agent.quality.adapters.harbor import RUNTIME_RECORD_NAME
from aegis_agent.quality.models import ExecutionRecord

_REMOTE_VENV = PurePosixPath("/opt/aegis-venv")
_INHERITED_ENV = (
    "AEGIS_API_KEY",
    "AEGIS_BASE_URL",
    "AEGIS_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
)
_EXPLICIT_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class Aegis(BaseInstalledAgent):
    """Install the current Aegis wheel in a Harbor task and run one task."""

    capabilities = AgentCapabilities()
    CLI_FLAGS: ClassVar[list[CliFlag]] = [
        CliFlag("max_iterations", cli="--max-iterations", type="int", default=50),
        CliFlag("allow_dangerous_shell", cli="--allow-dangerous-shell", type="bool", default=False),
        CliFlag("subagents", cli="--subagents", type="bool", default=True),
    ]

    def __init__(self, *args: Any, source_dir: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._source_dir = (
            Path(source_dir).expanduser().resolve()
            if source_dir is not None
            else Path(__file__).resolve().parents[3]
        )

    @staticmethod
    def name() -> str:
        return "aegis"

    def version(self) -> str | None:
        return self._version or __version__

    def get_version_command(self) -> str | None:
        return f"{_REMOTE_VENV}/bin/aegis --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment,
            ("bash", "git", "python3", "python_pip", "python_venv", "ripgrep"),
        )
        wheel_path = await asyncio.to_thread(self._build_wheel)
        remote_wheel = f"/tmp/{wheel_path.name}"
        try:
            await environment.upload_file(wheel_path, remote_wheel)
            await self.exec_as_root(
                environment,
                command=(
                    "set -euo pipefail; "
                    f"python3 -m venv {shlex.quote(str(_REMOTE_VENV))}; "
                    f"{_REMOTE_VENV}/bin/pip install --disable-pip-version-check "
                    f"{shlex.quote(remote_wheel + '[observability]')}"
                ),
            )
        finally:
            for path in wheel_path.parent.iterdir():
                path.unlink(missing_ok=True)
            wheel_path.parent.rmdir()

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self.context_id is None:
            raise ValueError("Harbor did not assign Aegis a context_id/trial id")

        execution_id = str(self.context_id)
        logs_dir = self.environment_logs_dir
        record_path = logs_dir / RUNTIME_RECORD_NAME
        env = self._runtime_env(instruction, execution_id, record_path)
        flags = [
            f"--max-iterations {int(self._resolved_flags['max_iterations'])}",
            "--subagents" if self._resolved_flags.get("subagents") else "--no-subagents",
        ]
        if self._resolved_flags.get("allow_dangerous_shell"):
            flags.append("--allow-dangerous-shell")
        command = (
            "set -euo pipefail; "
            f"{_REMOTE_VENV}/bin/aegis run --cwd /app "
            "--no-skills --no-mcp --no-memory "
            f"{' '.join(flags)} 2>&1 | "
            f"stdbuf -oL tee {shlex.quote(str(logs_dir / 'aegis.txt'))}"
        )
        await self.exec_as_agent(environment, command=command, env=env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        record_path = self.logs_dir / RUNTIME_RECORD_NAME
        if not record_path.is_file():
            return
        try:
            record = ExecutionRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        usage = record.usage
        cache_tokens = usage.cache_tokens
        if cache_tokens is None:
            cache_parts = [usage.cache_read_tokens, usage.cache_write_tokens]
            cache_tokens = sum(value for value in cache_parts if value is not None)
            if not any(value is not None for value in cache_parts):
                cache_tokens = None
        input_tokens = usage.input_tokens_including_cache
        if input_tokens is None and usage.input_tokens is not None:
            input_tokens = usage.input_tokens + (cache_tokens or 0)
        context.n_input_tokens = input_tokens
        context.n_cache_tokens = cache_tokens
        context.n_output_tokens = usage.output_tokens
        context.cost_usd = usage.cost
        context.metadata = {
            "aegis_execution_id": record.identity.execution_id,
            "langfuse_trace_id": record.identity.trace_id,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "runtime_success": record.execution.success,
        }

    def _runtime_env(
        self,
        instruction: str,
        execution_id: str,
        record_path: PurePosixPath,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _INHERITED_ENV:
            value = self._get_env(key)
            if value is not None:
                env[key] = value

        # A host loopback proxy (for example 127.0.0.1:10808 in WSL) points
        # back to the task container after Harbor forwards it. Proxy settings
        # therefore require an explicit --ae value that is reachable from the
        # container instead of silently inheriting the Harbor process environment.
        explicit_env = self.extra_env
        for key in _EXPLICIT_PROXY_ENV:
            if key in explicit_env:
                env[key] = explicit_env[key]

        if self.model_name:
            provider, separator, model = self.model_name.partition("/")
            if not separator:
                model = provider
                provider = "openai"
            if provider == "anthropic":
                env["AEGIS_MODEL_BACKEND"] = "anthropic"
                env["ANTHROPIC_MODEL"] = model
            else:
                env["AEGIS_MODEL_BACKEND"] = "openai"
                env["AEGIS_MODEL"] = model
                if "AEGIS_API_KEY" not in env:
                    fallback = self._get_env("OPENAI_API_KEY")
                    if fallback:
                        env["AEGIS_API_KEY"] = fallback
                if "AEGIS_BASE_URL" not in env:
                    fallback = self._get_env("OPENAI_BASE_URL")
                    if fallback:
                        env["AEGIS_BASE_URL"] = fallback

        env.update(
            {
                "AEGIS_INSTRUCTION": instruction,
                "AEGIS_EXECUTION_ID": execution_id,
                "AEGIS_SESSION_ID": self.session_id or execution_id,
                "AEGIS_EXECUTION_RECORD_PATH": str(record_path),
                "AEGIS_EXECUTION_RECORDS_DIR": str(self.environment_logs_dir / "executions"),
                "AEGIS_RUN_KIND": "evaluation",
            }
        )
        return env

    def _build_wheel(self) -> Path:
        if not (self._source_dir / "pyproject.toml").is_file():
            raise FileNotFoundError(f"Aegis source directory is invalid: {self._source_dir}")
        output_dir = Path(tempfile.mkdtemp(prefix="aegis-harbor-wheel-"))
        try:
            completed = subprocess.run(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--out-dir",
                    str(output_dir),
                    str(self._source_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "failed to build Aegis wheel: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )
            wheels = list(output_dir.glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError(f"expected one Aegis wheel, found {len(wheels)}")
            return wheels[0]
        except BaseException:
            for path in output_dir.iterdir():
                path.unlink(missing_ok=True)
            output_dir.rmdir()
            raise


__all__ = ["Aegis"]
