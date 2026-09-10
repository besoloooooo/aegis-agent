from __future__ import annotations

from pathlib import PurePosixPath

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
