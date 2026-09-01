"""Atomic local persistence for :class:`ExecutionRecord`."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from aegis_agent.quality.models import ExecutionRecord

ENV_RECORDS_DIR = "AEGIS_EXECUTION_RECORDS_DIR"


def default_records_dir() -> Path:
    configured = os.environ.get(ENV_RECORDS_DIR)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aegis" / "quality" / "executions"


class ExecutionRecordStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory).expanduser() if directory else default_records_dir()

    def path_for(self, execution_id: str) -> Path:
        safe = "".join(char for char in execution_id if char.isalnum() or char in "-_")
        if not safe:
            raise ValueError("execution_id must contain a filename-safe character")
        return self.directory / f"{safe}.json"

    def save(self, record: ExecutionRecord, path: str | Path | None = None) -> Path:
        target = Path(path).expanduser() if path else self.path_for(record.identity.execution_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json(indent=2, exclude_none=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return target

    def load(self, path_or_id: str | Path) -> ExecutionRecord:
        candidate = Path(path_or_id).expanduser()
        path = candidate if candidate.is_file() else self.path_for(str(path_or_id))
        return ExecutionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int | None = None) -> list[ExecutionRecord]:
        if not self.directory.is_dir():
            return []
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if limit is not None:
            paths = paths[:limit]
        records: list[ExecutionRecord] = []
        for path in paths:
            try:
                records.append(ExecutionRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records


__all__ = ["ENV_RECORDS_DIR", "ExecutionRecordStore", "default_records_dir"]
