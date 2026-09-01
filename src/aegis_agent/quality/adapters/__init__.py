"""Adapters that merge external evaluation results into ExecutionRecord."""

from aegis_agent.quality.adapters.harbor import (
    finalize_harbor_job,
    finalize_harbor_trial,
)

__all__ = ["finalize_harbor_job", "finalize_harbor_trial"]
