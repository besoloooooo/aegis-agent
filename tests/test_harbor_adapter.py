from __future__ import annotations

import asyncio
import hashlib
import io
import os
import subprocess
import tarfile
import threading
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

Aegis = pytest.importorskip("aegis_agent.integrations.harbor").Aegis


_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _runtime_env(agent: Aegis) -> dict[str, str]:
    return agent._runtime_env(
        "test instruction",
        "execution-id",
        PurePosixPath("/logs/agent/execution-record.runtime.json"),
    )


@pytest.mark.parametrize("name", _PROXY_ENV_NAMES)
def test_runtime_env_does_not_inherit_host_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    name: str,
) -> None:
    monkeypatch.setenv(name, "http://127.0.0.1:10808")
    agent = Aegis(
        logs_dir=tmp_path,
        model_name="openai/qwen3.7-plus",
        extra_env={
            "AEGIS_API_KEY": "test-key",
            "AEGIS_BASE_URL": "https://example.invalid/v1",
        },
    )

    env = _runtime_env(agent)

    assert name not in env


@pytest.mark.parametrize("name", _PROXY_ENV_NAMES)
def test_runtime_env_forwards_explicit_agent_proxy(tmp_path, name: str) -> None:
    agent = Aegis(
        logs_dir=tmp_path,
        model_name="openai/qwen3.7-plus",
        extra_env={name: "http://proxy.internal:3128"},
    )

    env = _runtime_env(agent)

    assert env[name] == "http://proxy.internal:3128"


def test_runtime_env_forwards_model_timeout(tmp_path) -> None:
    agent = Aegis(
        logs_dir=tmp_path,
        model_name="openai/qwen3.7-plus",
        extra_env={"AEGIS_MODEL_TIMEOUT": "300"},
    )

    env = _runtime_env(agent)

    assert env["AEGIS_MODEL_TIMEOUT"] == "300"


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:10808",
        "http://localhost:10808",
        "http://[::1]:10808",
    ],
)
def test_explicit_loopback_proxy_is_rejected(tmp_path, proxy_url: str) -> None:
    agent = Aegis(
        logs_dir=tmp_path,
        model_name="openai/qwen3.7-plus",
        extra_env={"HTTPS_PROXY": proxy_url},
    )

    with pytest.raises(ValueError, match="resolves inside the Harbor task container"):
        _runtime_env(agent)


@pytest.mark.asyncio
async def test_install_forwards_explicit_proxy_and_pip_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    wheel_path = wheel_dir / "aegis_agent-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"test wheel")
    agent = Aegis(
        logs_dir=tmp_path / "logs",
        model_name="openai/qwen3.7-plus",
        extra_env={
            "HTTP_PROXY": "http://proxy.internal:3128",
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "PIP_INDEX_URL": "https://mirror.invalid/simple/",
            "PIP_DEFAULT_TIMEOUT": "120",
            "PIP_RETRIES": "10",
        },
    )
    environment = AsyncMock()
    agent.ensure_system_dependencies = AsyncMock()
    agent.exec_as_root = AsyncMock()
    agent._ensure_uv = AsyncMock(return_value="/opt/aegis-uv/uv")
    monkeypatch.setattr(agent, "_build_wheel", lambda: wheel_path)

    await agent.install(environment)

    proxy_env = {
        "HTTP_PROXY": "http://proxy.internal:3128",
        "HTTPS_PROXY": "http://proxy.internal:3128",
    }
    agent.ensure_system_dependencies.assert_awaited_once_with(
        environment,
        ("bash", "git", "ca_certificates", "coreutils", "ripgrep"),
        env=proxy_env,
    )
    install_call = agent.exec_as_root.await_args
    assert install_call.kwargs["env"] == {
        **proxy_env,
        "PIP_INDEX_URL": "https://mirror.invalid/simple/",
        "PIP_DEFAULT_TIMEOUT": "120",
        "PIP_RETRIES": "10",
        "UV_INDEX_URL": "https://mirror.invalid/simple/",
        "UV_HTTP_TIMEOUT": "120",
        "UV_HTTP_RETRIES": "10",
    }
    assert not wheel_dir.exists()


@pytest.mark.asyncio
async def test_install_adds_stable_linux_host_alias_when_requested(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    wheel_path = wheel_dir / "aegis_agent-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"test wheel")
    agent = Aegis(
        logs_dir=tmp_path / "logs",
        model_name="openai/qwen3.7-plus",
        extra_env={"HTTPS_PROXY": "http://host.docker.internal:18080"},
    )
    environment = AsyncMock()
    agent.ensure_system_dependencies = AsyncMock()
    agent.exec_as_root = AsyncMock()
    agent._ensure_uv = AsyncMock(return_value="/opt/aegis-uv/uv")
    monkeypatch.setattr(agent, "_build_wheel", lambda: wheel_path)

    await agent.install(environment)

    assert agent.exec_as_root.await_count == 2
    alias_call, install_call = agent.exec_as_root.await_args_list
    assert "/proc/net/route" in alias_call.kwargs["command"]
    assert "host.docker.internal" in alias_call.kwargs["command"]
    assert install_call.kwargs["env"] == {
        "HTTPS_PROXY": "http://host.docker.internal:18080"
    }


def test_uv_install_env_preserves_pip_options_and_explicit_uv_precedence(tmp_path) -> None:
    agent = Aegis(
        logs_dir=tmp_path / "logs",
        extra_env={
            "PIP_EXTRA_INDEX_URL": "https://extra.invalid/simple",
            "PIP_TRUSTED_HOST": "mirror.invalid",
            "PIP_FIND_LINKS": "/wheels",
            "PIP_CERT": "/custom-ca.pem",
            "REQUESTS_CA_BUNDLE": "/requests-ca.pem",
            "PIP_DEFAULT_TIMEOUT": "120",
            "UV_HTTP_TIMEOUT": "90",
            "UV_PYTHON_INSTALL_MIRROR": "https://python-mirror.invalid",
        },
    )

    env = agent._pip_install_env({})

    assert env["UV_EXTRA_INDEX_URL"] == "https://extra.invalid/simple"
    assert env["UV_INSECURE_HOST"] == "mirror.invalid"
    assert env["UV_FIND_LINKS"] == "/wheels"
    assert env["SSL_CERT_FILE"] == "/custom-ca.pem"
    assert env["UV_HTTP_TIMEOUT"] == "90"
    assert env["UV_PYTHON_INSTALL_MIRROR"] == "https://python-mirror.invalid"


@pytest.mark.parametrize("fail_python", [False, True])
def test_install_uses_managed_python_and_stops_on_provisioning_failure(
    tmp_path, fail_python: bool
) -> None:
    agent = Aegis(logs_dir=tmp_path / "logs", extra_env={"PIP_NO_INDEX": "true"})
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = --version ]; then echo "uv 0.11.1"; exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$UV_TEST_LOG"\n'
        'printf "%s\\n" "$UV_PYTHON_INSTALL_DIR" >> "$UV_TEST_PYTHON_DIR"\n'
        + ('if [ "$1" = python ]; then exit 12; fi\n' if fail_python else "")
    )
    uv.chmod(0o755)
    log = tmp_path / "commands"
    python_dir = tmp_path / "python-dir"

    completed = subprocess.run(
        ["bash", "-c", agent._install_command("/tmp/aegis test.whl")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "UV_TEST_LOG": str(log),
            "UV_TEST_PYTHON_DIR": str(python_dir),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    commands = log.read_text().splitlines()
    assert commands[0] == "python install --no-bin 3.11"
    assert set(python_dir.read_text().splitlines()) == {"/opt/aegis-python"}
    if fail_python:
        assert completed.returncode == 12
        assert len(commands) == 1
    else:
        assert completed.returncode == 0, completed.stderr
        assert commands[1:] == [
            "venv --managed-python --python 3.11 /opt/aegis-venv",
            (
                "pip install --python /opt/aegis-venv/bin/python --no-index "
                "/tmp/aegis test.whl[observability]"
            ),
        ]


@pytest.mark.parametrize("value", ["false", "0", "off"])
def test_pip_no_index_false_does_not_disable_index(tmp_path, value: str) -> None:
    agent = Aegis(logs_dir=tmp_path, extra_env={"PIP_NO_INDEX": value})
    assert " --no-index" not in agent._install_command("/tmp/aegis.whl")


def test_invalid_pip_no_index_fails_explicitly(tmp_path) -> None:
    agent = Aegis(logs_dir=tmp_path, extra_env={"PIP_NO_INDEX": "typo"})
    with pytest.raises(ValueError, match="PIP_NO_INDEX"):
        agent._install_command("/tmp/aegis.whl")


@pytest.mark.parametrize("valid_checksum", [False, True])
def test_host_bootstrap_verifies_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch, valid_checksum: bool
) -> None:
    from aegis_agent.integrations import harbor

    archive = tmp_path / "uv.tar.gz"
    payload = b'#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_TEST_LOG"\n'
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("uv-x86_64-unknown-linux-musl/uv")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setitem(
        harbor._UV_CHECKSUMS, "x86_64", checksum if valid_checksum else "0" * 64
    )
    monkeypatch.setattr(harbor, "urlopen", lambda *a, **kw: io.BytesIO(archive.read_bytes()))

    if valid_checksum:
        binary = Aegis._download_uv("x86_64")
        try:
            assert binary.read_bytes() == payload
        finally:
            binary.unlink()
            binary.parent.rmdir()
    else:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            Aegis._download_uv("x86_64")


@pytest.mark.parametrize("version", ["0.7.13", "0.11.1", "0.11.10"])
def test_probe_reuses_only_compatible_uv(tmp_path, monkeypatch, version: str) -> None:
    from aegis_agent.integrations import harbor

    monkeypatch.setattr(harbor, "_REMOTE_UV", tmp_path / "managed")
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\necho "uv {version}"\n')
    uv.chmod(0o755)
    completed = subprocess.run(
        ["bash", "-c", Aegis._uv_probe_command()],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    if version == "0.11.1":
        assert completed.stdout.strip() == f"ready:{uv}"
    else:
        assert completed.stdout.startswith("download:")


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_fails", [False, True])
async def test_uv_host_staging_cleans_local_binary(tmp_path, monkeypatch, upload_fails):
    staging = tmp_path / "staging"
    staging.mkdir()
    binary = staging / "uv"
    binary.write_bytes(b"verified binary")
    agent = Aegis(logs_dir=tmp_path / "logs")
    agent.exec_as_root = AsyncMock(return_value=SimpleNamespace(stdout="download:x86_64\n"))
    monkeypatch.setattr(agent, "_download_uv", lambda arch: binary)
    environment = AsyncMock()
    if upload_fails:
        environment.upload_file.side_effect = RuntimeError("upload failed")
        with pytest.raises(RuntimeError, match="upload failed"):
            await agent._ensure_uv(environment)
    else:
        assert await agent._ensure_uv(environment) == "/opt/aegis-uv/uv"
        environment.upload_file.assert_awaited_once_with(binary, "/tmp/staging-uv")
        assert "install -m 755" in agent.exec_as_root.await_args.kwargs["command"]
    assert not staging.exists()


@pytest.mark.asyncio
async def test_uv_reuse_needs_no_host_download(tmp_path, monkeypatch):
    agent = Aegis(logs_dir=tmp_path / "logs")
    agent.exec_as_root = AsyncMock(return_value=SimpleNamespace(stdout="ready:/bin/uv\n"))
    environment = AsyncMock()
    assert await agent._ensure_uv(environment) == "/bin/uv"
    environment.upload_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_host_download_cleans_late_thread_result(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    binary = staging / "uv"
    binary.write_bytes(b"verified binary")
    agent = Aegis(logs_dir=tmp_path / "logs")
    agent.exec_as_root = AsyncMock(return_value=SimpleNamespace(stdout="download:x86_64\n"))
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def download(arch):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise TimeoutError("test worker was not released")
        return binary

    monkeypatch.setattr(agent, "_download_uv", download)
    task = asyncio.create_task(agent._ensure_uv(AsyncMock()))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    async with asyncio.timeout(2):
        while staging.exists():
            await asyncio.sleep(0.01)
