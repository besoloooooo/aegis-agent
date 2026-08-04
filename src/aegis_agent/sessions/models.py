"""Session data models.

Only the session *metadata* record lives here.  The conversational messages a
session contains are the canonical :class:`~aegis_agent.models.base.Message`
objects from the models layer — the repository stores those directly rather
than defining a parallel message record type.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    """Metadata for one conversation session."""

    id: str
    created_at: float = field(default_factory=time.time)
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["Session"]
