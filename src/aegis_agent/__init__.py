"""Aegis Agent — a lightweight, recoverable, extensible Agent Runtime.

Extracted, simplified and modularized from the core runtime behaviour of
Hermes (see ``docs/source-map.md``).  This package deliberately keeps a clean
one-way dependency direction: ``cli → runtime → models/tools/context/sessions``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
