"""Persistent state: sent-signal registry (duplicate prevention) + per-timeframe scan progress.

File layout (``state/sent_signals.json``, version 2; version-1 files load fine)::

    {"version": 2,
     "sent":  {signal_id: {"sent_at": ISO-8601 UTC}},
     "scans": {"1h": {"last_candle_open_ms": 123, "completed_at": ISO-8601 UTC}, ...}}

Safety properties
* Writes are atomic (temp file + ``os.replace``).
* ``save()`` takes an exclusive advisory file lock, re-reads the file on disk and MERGES (union of sent
  ids, newest scan progress) before writing, so two processes sharing a state file cannot erase each
  other's entries.  In GitHub Actions the workflow ``concurrency`` group additionally serialises runs.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Optional

try:  # POSIX only (GitHub runners are Linux); degrade gracefully elsewhere
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

log = logging.getLogger("state")


def _read_file(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    sent = data.get("sent", {})
    scans = data.get("scans", {})
    sent = {str(k): dict(v) for k, v in sent.items() if isinstance(v, dict)} if isinstance(sent, dict) else {}
    scans = {str(k): dict(v) for k, v in scans.items() if isinstance(v, dict)} if isinstance(scans, dict) else {}
    return sent, scans


class SentSignalStore:
    def __init__(self, path: str, retention_days: int = 14) -> None:
        self.path = path
        self.retention = timedelta(days=retention_days)
        self._sent: Dict[str, Dict[str, str]] = {}
        self._scans: Dict[str, Dict[str, object]] = {}
        self._removed: set = set()
        self._load()

    def _load(self) -> None:
        try:
            self._sent, self._scans = _read_file(self.path)
        except FileNotFoundError:
            log.info("No state file yet (%s) - starting empty", self.path)
        except (ValueError, OSError, AttributeError, TypeError) as exc:
            # A corrupt state file must not crash the scan; the lookback window bounds re-sends.
            log.warning("State file unreadable (%s) - starting empty", type(exc).__name__)

    # ---- sent signals -------------------------------------------------------------------------
    def __contains__(self, signal_id: str) -> bool:
        return signal_id in self._sent

    def __len__(self) -> int:
        return len(self._sent)

    def __bool__(self) -> bool:  # an empty registry is still a valid store (avoid `store or ...` traps)
        return True

    def add(self, signal_id: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._sent[signal_id] = {"sent_at": now.isoformat()}
        self._removed.discard(signal_id)

    def prune(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - self.retention
        drop = []
        for sid, meta in self._sent.items():
            try:
                ts = datetime.fromisoformat(meta.get("sent_at", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    drop.append(sid)
            except ValueError:
                drop.append(sid)
        for sid in drop:
            del self._sent[sid]
            self._removed.add(sid)
        return len(drop)

    # ---- scan progress ------------------------------------------------------------------------
    def last_scanned_candle(self, timeframe: str) -> Optional[int]:
        """Open time (ms) of the newest candle whose scan fully completed, or None."""
        value = self._scans.get(timeframe, {}).get("last_candle_open_ms")
        return int(value) if isinstance(value, (int, float)) else None

    def mark_scanned(self, timeframe: str, candle_open_ms: int, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        prev = self.last_scanned_candle(timeframe)
        if prev is not None and prev >= candle_open_ms:
            return
        self._scans[timeframe] = {"last_candle_open_ms": int(candle_open_ms), "completed_at": now.isoformat()}

    # ---- persistence --------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        if fcntl is None:  # pragma: no cover
            yield
            return
        with open(self.path + ".lock", "a") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def save(self) -> None:
        """Merge with whatever is on disk, then write atomically under an exclusive lock."""
        directory = os.path.dirname(os.path.abspath(self.path))
        with self._locked():
            try:
                disk_sent, disk_scans = _read_file(self.path)
            except (FileNotFoundError, ValueError, OSError, AttributeError, TypeError):
                disk_sent, disk_scans = {}, {}
            for sid, meta in disk_sent.items():
                if sid not in self._sent and sid not in self._removed:
                    self._sent[sid] = meta
            for tf, meta in disk_scans.items():
                mine = self._scans.get(tf)
                if mine is None or (
                    isinstance(meta.get("last_candle_open_ms"), (int, float))
                    and meta["last_candle_open_ms"] > mine.get("last_candle_open_ms", -1)
                ):
                    self._scans[tf] = meta
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sent_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": 2, "sent": self._sent, "scans": self._scans}, fh, indent=1, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
