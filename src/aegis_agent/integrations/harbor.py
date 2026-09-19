"""Harbor 0.22 custom installed-agent adapter for Aegis.

Harbor is intentionally an optional dependency. Import this module only from a
Harbor environment, for example with ``-a aegis_agent.integrations.harbor:Aegis``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from textwrap import dedent
from typing import Any, ClassVar
from urllib.parse import urlsplit
from urllib.request import urlopen

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
_REMOTE_PYTHON = PurePosixPath("/opt/aegis-python")
_REMOTE_UV = PurePosixPath("/opt/aegis-uv")
# Checksums published alongside the official uv 0.11.1 release archives.
_UV_VERSION = "0.11.1"
_UV_CHECKSUMS = {
    "x86_64": "4e949471a95b37088a1ff1a585f69abed4d3cd3f921f50709a46b6ba62986d38",
    "aarch64": "bd04ffce77ee8d77f39823c13606183581847c2f5dcd704f2ea0f15e376b1a27",
}
_INHERITED_ENV = (
    "AEGIS_API_KEY",
    "AEGIS_BASE_URL",
    "AEGIS_MODEL",
    "AEGIS_MODEL_TIMEOUT",
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
_PROXY_URL_ENV = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)
_CONTAINER_HOST_ALIAS = "host.docker.internal"
_EXPLICIT_PIP_ENV = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_DEFAULT_TIMEOUT",
    "PIP_RETRIES",
    "PIP_TRUSTED_HOST",
    "PIP_CERT",
    "PIP_NO_INDEX",
    "PIP_FIND_LINKS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)
_EXPLICIT_UV_ENV = (
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_HTTP_TIMEOUT",
    "UV_HTTP_RETRIES",
    "UV_INSECURE_HOST",
    "UV_FIND_LINKS",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_SYSTEM_CERTS",
)
_PIP_TO_UV_ENV = {
    "PIP_INDEX_URL": "UV_INDEX_URL",
    "PIP_EXTRA_INDEX_URL": "UV_EXTRA_INDEX_URL",
    "PIP_DEFAULT_TIMEOUT": "UV_HTTP_TIMEOUT",
    "PIP_RETRIES": "UV_HTTP_RETRIES",
    "PIP_TRUSTED_HOST": "UV_INSECURE_HOST",
    "PIP_FIND_LINKS": "UV_FIND_LINKS",
}


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
        proxy_env = self._explicit_proxy_env()
        if any(_proxy_hostname(value) == _CONTAINER_HOST_ALIAS for value in proxy_env.values()):
            await self._ensure_container_host_alias(environment)
        await self.ensure_system_dependencies(
            environment,
            ("bash", "git", "ca_certificates", "coreutils", "ripgrep"),
            env=proxy_env or None,
        )
        uv_command = await self._ensure_uv(environment)
        wheel_path = await asyncio.to_thread(self._build_wheel)
        remote_wheel = f"/tmp/{wheel_path.name}"
        try:
            await environment.upload_file(wheel_path, remote_wheel)
            await self.exec_as_root(
                environment,
                command=self._install_command(remote_wheel, uv_command=uv_command),
                env=self._pip_install_env(proxy_env) or None,
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
        env.update(self._explicit_proxy_env())

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

    def _explicit_proxy_env(self) -> dict[str, str]:
        explicit_env = self.extra_env
        proxy_env = {
            key: explicit_env[key] for key in _EXPLICIT_PROXY_ENV if key in explicit_env
        }
        for key, value in proxy_env.items():
            if key in _PROXY_URL_ENV and _uses_loopback_host(value):
                raise ValueError(
                    f"{key} points to loopback, which resolves inside the Harbor task "
                    "container; provide a proxy URL reachable from the container"
                )
        return proxy_env

    def _pip_install_env(self, proxy_env: dict[str, str]) -> dict[str, str]:
        explicit_env = self.extra_env
        env = {
            **proxy_env,
            **{
                key: explicit_env[key]
                for key in _EXPLICIT_PIP_ENV
                if key in explicit_env
            },
        }
        for pip_name, uv_name in _PIP_TO_UV_ENV.items():
            if pip_name in explicit_env:
                env[uv_name] = explicit_env[pip_name]
        # Match pip's explicit certificate override, then requests' CA bundle.
        for name in ("PIP_CERT", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
            if name in explicit_env:
                env["SSL_CERT_FILE"] = explicit_env[name]
                break
        env.update({key: explicit_env[key] for key in _EXPLICIT_UV_ENV if key in explicit_env})
        return env

    async def _ensure_uv(self, environment: BaseEnvironment) -> str:
        result = await self.exec_as_root(environment, command=self._uv_probe_command())
        probe = (result.stdout or "").strip()
        if probe.startswith("ready:"):
            return probe.removeprefix("ready:")
        arch = probe.removeprefix("download:")
        if arch == "arm64":
            arch = "aarch64"
        if arch not in _UV_CHECKSUMS:
            raise RuntimeError(f"Unsupported architecture for Aegis uv bootstrap: {arch}")
        download = asyncio.create_task(asyncio.to_thread(self._download_uv, arch))
        try:
            binary = await asyncio.shield(download)
        except asyncio.CancelledError:
            # A worker thread cannot be stopped by asyncio cancellation. Reap
            # its temporary output when the bounded download eventually exits.
            def discard_download(completed: asyncio.Task[Path]) -> None:
                if completed.cancelled():
                    return
                if completed.exception() is not None:
                    return
                staged_binary = completed.result()
                staged_binary.unlink(missing_ok=True)
                staged_binary.parent.rmdir()

            download.add_done_callback(discard_download)
            raise
        remote_binary = f"/tmp/{binary.parent.name}-uv"
        try:
            await environment.upload_file(binary, remote_binary)
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; umask 022; "
                    f"mkdir -p {shlex.quote(str(_REMOTE_UV))}; "
                    f"install -m 755 {shlex.quote(remote_binary)} "
                    f"{shlex.quote(str(_REMOTE_UV / 'uv'))}; "
                    f"rm -f -- {shlex.quote(remote_binary)}"
                ),
            )
        finally:
            binary.unlink(missing_ok=True)
            binary.parent.rmdir()
        return str(_REMOTE_UV / "uv")

    @staticmethod
    def _download_uv(arch: str) -> Path:
        archive_name = f"uv-{arch}-unknown-linux-musl"
        url = (
            f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/"
            f"{archive_name}.tar.gz"
        )
        # Download on the Harbor host, whose proxy settings work independently
        # of the task network. Never extract arbitrary archive paths or links.
        for attempt in range(3):
            try:
                deadline = time.monotonic() + 120
                with urlopen(url, timeout=60) as response:
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Aegis uv archive download exceeded 120 seconds")
                        chunk = response.read1(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 128 * 1024 * 1024:
                            raise RuntimeError("Aegis uv archive exceeds 128 MiB")
                        chunks.append(chunk)
                    archive = b"".join(chunks)
                break
            except OSError:
                if attempt == 2:
                    raise
        if hashlib.sha256(archive).hexdigest() != _UV_CHECKSUMS[arch]:
            raise RuntimeError("Aegis uv bootstrap archive checksum mismatch")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = tar.getmember(f"{archive_name}/uv")
            if not member.isfile():
                raise RuntimeError("Aegis uv archive does not contain a regular uv binary")
            binary_file = tar.extractfile(member)
            if binary_file is None:
                raise RuntimeError("Aegis uv archive is missing its binary")
            binary_contents = binary_file.read()
        directory = Path(tempfile.mkdtemp(prefix="aegis-harbor-uv-"))
        binary = directory / "uv"
        try:
            binary.write_bytes(binary_contents)
            return binary
        except BaseException:
            binary.unlink(missing_ok=True)
            directory.rmdir()
            raise

    @staticmethod
    def _uv_probe_command() -> str:
        return dedent(f"""\
            set -euo pipefail
            uv_is_compatible() {{
                local version
                version=$("$1" --version) || return 1
                [[ "$version" == "uv {_UV_VERSION}" || "$version" == "uv {_UV_VERSION} "* ]]
            }}
            if command -v uv >/dev/null 2>&1 && uv_is_compatible "$(command -v uv)"; then
                printf 'ready:%s\\n' "$(command -v uv)"
            elif [ -x {shlex.quote(str(_REMOTE_UV / 'uv'))} ] && uv_is_compatible {shlex.quote(str(_REMOTE_UV / 'uv'))}; then
                printf 'ready:%s\\n' {shlex.quote(str(_REMOTE_UV / 'uv'))}
            else
                printf 'download:%s\\n' "$(uname -m)"
            fi
            """)

    def _install_command(self, remote_wheel: str, *, uv_command: str = "uv") -> str:
        no_index = self.extra_env.get("PIP_NO_INDEX", "").lower()
        if no_index not in {"", "0", "1", "false", "true", "no", "yes", "off", "on"}:
            raise ValueError("PIP_NO_INDEX must be a boolean value")
        index_flag = " --no-index" if no_index in {"1", "true", "yes", "on"} else ""
        # Shared /opt storage keeps the managed interpreter traversable by the
        # non-root agent. Never replace or install into the task's system Python.
        return dedent(f"""\
            set -euo pipefail
            umask 022
            export UV_PYTHON_INSTALL_DIR={shlex.quote(str(_REMOTE_PYTHON))}
            export UV_NO_CONFIG=1
            uv_cmd={shlex.quote(uv_command)}
            "$uv_cmd" python install --no-bin 3.11
            "$uv_cmd" venv --managed-python --python 3.11 {shlex.quote(str(_REMOTE_VENV))}
            "$uv_cmd" pip install --python {shlex.quote(str(_REMOTE_VENV / 'bin/python'))}{index_flag} \\
                {shlex.quote(remote_wheel + '[observability]')}
            """)

    async def _ensure_container_host_alias(self, environment: BaseEnvironment) -> None:
        """Define Docker's stable host alias when native Linux omits it.

        Docker Desktop defines ``host.docker.internal`` automatically, while a
        native Linux Docker engine generally does not. Only create the alias
        when the user explicitly selected it in a proxy URL.
        """
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"if ! getent hosts {_CONTAINER_HOST_ALIAS} >/dev/null 2>&1; then "
                "gateway_hex=$(awk '$2 == \"00000000\" {print $3; exit}' /proc/net/route); "
                'test -n "$gateway_hex"; '
                "gateway=$(printf '%d.%d.%d.%d' "
                '"0x${gateway_hex:6:2}" "0x${gateway_hex:4:2}" '
                '"0x${gateway_hex:2:2}" "0x${gateway_hex:0:2}"); '
                f"printf '%s\\t%s\\n' \"$gateway\" {_CONTAINER_HOST_ALIAS} >> /etc/hosts; "
                "fi"
            ),
        )

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


def _uses_loopback_host(value: str) -> bool:
    hostname = _proxy_hostname(value)
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _proxy_hostname(value: str) -> str | None:
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


__all__ = ["Aegis"]
