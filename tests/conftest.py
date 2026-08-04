"""Shared fixtures for Aegis Agent tests."""

from __future__ import annotations

import pytest

from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime
from aegis_agent.sessions.memory_store import InMemorySessionRepository


@pytest.fixture
def repository() -> InMemorySessionRepository:
    return InMemorySessionRepository()


@pytest.fixture
def make_runtime(repository):
    """Factory: build an AgentRuntime with a scripted fake provider."""

    def _make(script=None, *, max_iterations=10, cwd=None, chunk_text=False):
        provider = FakeModelProvider(script=list(script) if script else None, chunk_text=chunk_text)
        runtime = AgentRuntime.with_defaults(
            provider=provider,
            repository=repository,
            max_iterations=max_iterations,
            cwd=cwd,
        )
        return runtime, provider

    return _make


@pytest.fixture
def text_reply():
    return lambda text: FakeReply(text=text)
