"""JSONL file-backed audit log for provider API calls.

Stores one JSON object per line (append-only).  Designed for low
overhead (single line write per API call), human-readability
(``cat audit.jsonl | jq``), and programmatic querying via the
``query()`` and ``stats()`` helpers.

Default location: ``~/.one_finance_data/audit/audit.jsonl``.
Default retention: 30 days (pruned on startup).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from onefinance._clock import get_clock
from onefinance.audit._stats import AuditStatsAccumulator
from onefinance.audit.models import AuditEntry, AuditStats

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = "~/.one_finance_data/audit"
_DEFAULT_RETENTION_DAYS = 30


class AuditLog:
    """JSONL file-backed audit log for provider API calls.

    Parameters
    ----------
    log_path:
        Path to the JSONL log file.  If a directory is given,
        ``audit.jsonl`` is appended.  Defaults to
        ``~/.one_finance_data/audit/audit.jsonl``.
    retention_days:
        Entries older than this are pruned on startup.
        Set to ``0`` to disable pruning.
    enabled:
        If ``False``, ``record()`` is a no-op.  Useful for tests
        or when audit overhead is unwanted.
    """

    def __init__(
        self,
        log_path: str | Path | None = None,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._retention_days = retention_days
        self._path: Path | None = None
        self._fh: TextIO | None = None

        if not enabled:
            return

        resolved = Path(log_path or _DEFAULT_LOG_DIR).expanduser()
        if resolved.is_dir() or not resolved.suffix:
            resolved = resolved / "audit.jsonl"
        self._path = resolved

        # Auto-prune old entries
        if retention_days > 0 and self._path.exists():
            pruned = self._prune()
            if pruned > 0:
                logger.debug(
                    "Pruned %d audit entries older than %d days",
                    pruned,
                    retention_days,
                )

    # -------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------

    def record(self, entry: AuditEntry) -> None:
        """Append an audit entry as a single JSON line.

        No-op if the log is disabled. Keeps the file handle open across
        calls (line-buffered, so each line is still flushed immediately)
        instead of paying an open/close syscall pair per audit entry —
        this is on the hot path for every provider call.
        """
        if not self._enabled or self._path is None:
            return

        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._path, "a", buffering=1)
            line = json.dumps(entry.to_dict(), separators=(",", ":"))
            self._fh.write(line + "\n")
        except Exception:
            logger.debug("Failed to write audit entry", exc_info=True)

    # -------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------

    def query(
        self,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        request_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries with optional filters.

        Returns newest-first, up to ``limit`` entries.
        """
        if not self._enabled or self._path is None or not self._path.exists():
            return []

        entries: list[AuditEntry] = []
        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Apply filters
            if provider is not None and obj.get("provider") != provider:
                continue
            if endpoint is not None and obj.get("endpoint") != endpoint:
                continue
            if status is not None and obj.get("status") != status:
                continue
            if symbol is not None and (obj.get("symbol") or "").upper() != symbol.upper():
                continue
            if request_id is not None and obj.get("request_id") != request_id:
                continue
            if since is not None:
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts is not None and ts < since:
                    continue

            entries.append(_dict_to_entry(obj))

        # Newest-first, limited
        entries.reverse()
        return entries[:limit]

    # -------------------------------------------------------------------
    # Aggregate stats
    # -------------------------------------------------------------------

    def stats(
        self,
        *,
        since: datetime | None = None,
    ) -> AuditStats:
        """Compute aggregate stats over a time range.

        Parameters
        ----------
        since:
            Start of the stats period.  Defaults to 24 hours ago.
        """
        if not self._enabled or self._path is None or not self._path.exists():
            return AuditStats()

        if since is None:
            since = get_clock().now() - timedelta(days=1)

        now = get_clock().now()

        accumulator = AuditStatsAccumulator()

        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            ts = _parse_ts(obj.get("timestamp", ""))
            if ts is not None and ts < since:
                continue

            accumulator.add(obj)

        return accumulator.build(period_start=since, period_end=now)

    # -------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------

    def _prune(self) -> int:
        """Remove entries older than ``retention_days``.

        Rewrites the file in-place, keeping only recent entries.
        Returns the number of entries removed.
        """
        if self._path is None or not self._path.exists():
            return 0

        cutoff = get_clock().now() - timedelta(days=self._retention_days)
        kept: list[str] = []
        pruned = 0

        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts is not None and ts < cutoff:
                    pruned += 1
                    continue
            except json.JSONDecodeError:
                pruned += 1
                continue
            kept.append(raw)

        if pruned > 0:
            with open(self._path, "w") as f:
                for line in kept:
                    f.write(line if line.endswith("\n") else line + "\n")

        return pruned

    def clear(self) -> None:
        """Remove all entries from the audit log."""
        self._close_fh()
        if self._path is not None and self._path.exists():
            self._path.write_text("")

    def close(self) -> None:
        """Close the open audit-log file handle, if any."""
        self._close_fh()

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                logger.debug("Failed to close audit log handle", exc_info=True)
            self._fh = None

    @property
    def enabled(self) -> bool:
        """Whether this audit log is active."""
        return self._enabled

    @property
    def path(self) -> Path | None:
        """Path to the log file, or None if disabled."""
        return self._path

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _read_lines(self) -> list[str]:
        """Read all lines from the log file."""
        if self._path is None or not self._path.exists():
            return []
        with open(self._path) as f:
            return [line.strip() for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp string to datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _dict_to_entry(obj: dict[str, Any]) -> AuditEntry:
    """Convert a JSON dict to an AuditEntry."""
    return AuditEntry(
        timestamp=_parse_ts(obj.get("timestamp", "")) or get_clock().now(),
        request_id=obj.get("request_id", ""),
        endpoint=obj.get("endpoint", ""),
        provider=obj.get("provider", ""),
        symbol=obj.get("symbol"),
        status=obj.get("status", ""),
        latency_ms=obj.get("latency_ms", 0),
        error_code=obj.get("error_code"),
        error_message=obj.get("error_message"),
        tier_position=obj.get("tier_position", 0),
        tier_total=obj.get("tier_total", 1),
        http_status=obj.get("http_status"),
        cache_key=obj.get("cache_key"),
        is_fallback=bool(obj.get("is_fallback", False)),
        stale_age_s=obj.get("stale_age_s"),
    )
