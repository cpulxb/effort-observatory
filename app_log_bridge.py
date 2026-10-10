"""Read the desktop's local debug log and save minimal response lifecycle events.

This module never enables logging, changes client configuration, or edits the
desktop log. Native App use requires analytics.enabled=false before restart;
otherwise the desktop also sends the same `safe.message` to its remote sink.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from observed_launcher import MAX_LINE, Recorder, atomic_json


LOG_NAME = re.compile(
    r"^codex-desktop-(?P<session>[0-9a-f-]{36})-(?P<pid>\d+)-t0-"
    r"i(?P<instance>\d+)-(?P<started>\d{6})-(?P<segment>\d+)\.log$"
)
PREFIX = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z debug "
    r"\[AppServerConnection\] Codex CLI stderr message="
)


def desktop_stderr_chunk(line: str):
    """Only the known desktop logger event may supply a Rust stderr chunk."""
    match = PREFIX.match(line)
    if not match:
        return None
    try:
        chunk, end = json.JSONDecoder().raw_decode(line[match.end():])
    except ValueError:
        return None
    if not isinstance(chunk, str) or line[match.end() + end:].strip():
        return None
    return chunk


class StderrAssembler:
    """Recover adjacent JSON records across trimmed Node stderr data chunks."""

    def __init__(self, recorder, max_bytes=MAX_LINE):
        self.recorder = recorder
        self.max_bytes = max_bytes
        self.pending = ""
        self.errors = 0

    def feed(self, chunk):
        if self.pending and not self.pending.lstrip().startswith("{") and chunk.lstrip().startswith("{"):
            self.pending = ""
        self.pending += chunk
        if len(self.pending.encode("utf-8")) > self.max_bytes:
            self.pending = ""
            self.errors += 1
            return
        decoder = json.JSONDecoder()
        while self.pending:
            self.pending = self.pending.lstrip()
            if not self.pending:
                return
            # LOG_FORMAT=json is forced by App. Skip a non-JSON diagnostic only
            # when its line has finished; do not parse text that resembles SSE.
            if not self.pending.startswith("{"):
                newline = self.pending.find("\n")
                if newline < 0:
                    return
                self.pending = self.pending[newline + 1:]
                continue
            try:
                _, end = decoder.raw_decode(self.pending)
            except ValueError:
                # A JSON log string cannot contain a literal newline. If one is
                # present the record is malformed, rather than an incomplete
                # pretty-printed record (Rust's JSON formatter is one line).
                newline = self.pending.find("\n")
                if newline >= 0:
                    self.pending = self.pending[newline + 1:]
                    self.errors += 1
                    continue
                return
            self.recorder.line(self.pending[:end].encode("utf-8"))
            self.pending = self.pending[end:]


class AppLogBridge:
    """Poll local desktop main-process logs without reading any client memory.

    Existing files start at EOF on first installation. Files discovered later
    start at byte zero, and offsets persist across observer restarts. A fixed
    App run directory must also be the parent of CODEX_ROLLOUT_TRACE_ROOT/native,
    so the collector joins both event streams by exact response ID.
    """

    def __init__(self, log_root, run_dir, *, run_id="desktop-native", from_start=False):
        self.log_root = Path(log_root).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.recorder = Recorder(self.run_dir, "Codex App", run_id)
        self.state_path = self.run_dir / "desktop-log-offsets.json"
        self.positions = {}
        self.streams = {}
        self.pending_starts = {}
        self.from_start = from_start
        self.initial = True
        self.status = dict(running=False, files=0, saved=0, errors=0, rotations=0,
                           last_scan=None, error=None)
        try:
            state = json.loads(self.state_path.read_text())
            if isinstance(state, dict) and state.get("log_root") == str(self.log_root):
                self.positions = state.get("positions", {})
                self.initial = False
        except (OSError, ValueError):
            pass

    def _error(self, message):
        self.status["errors"] += 1
        self.status["error"] = message

    def scan_once(self):
        self.status["running"] = True
        self.status["last_scan"] = time.time()
        files = []
        for path in self.log_root.glob("*/*/*/codex-desktop-*-t0-*.log"):
            if LOG_NAME.match(path.name) and path.resolve().is_relative_to(self.log_root):
                try:
                    files.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
        self.status["files"] = len(files)
        for _, path in sorted(files):
            try:
                self._read(path)
            except (OSError, ValueError, TypeError):
                self._error("desktop local log could not be read")
        self.initial = False
        # Keep incomplete Rust records only in memory. Persist a rewind point,
        # so a restart can reconstruct them from the original desktop log.
        persisted = {key: dict(value) for key, value in self.positions.items()}
        for checkpoints in self.pending_starts.values():
            for key, (offset, identity, anchor) in checkpoints.items():
                persisted[key] = {"offset": offset, "identity": identity, "anchor": anchor}
        atomic_json(self.state_path, {"log_root": str(self.log_root), "positions": persisted})
        self.status["saved"] = self.recorder.saved
        return dict(self.status)

    def _read(self, path):
        key = str(path.relative_to(self.log_root))
        name = LOG_NAME.match(path.name)
        # Rotation segments share a stderr stream; different Electron launches
        # must never complete one another's partial records.
        stream_key = (name["session"], name["pid"], name["instance"], name["started"])
        assembler = self.streams.setdefault(stream_key, StderrAssembler(self.recorder))
        with path.open("rb") as source:
            stat = os.fstat(source.fileno())
            identity = [stat.st_dev, stat.st_ino]
            saved = self.positions.get(key)
            offset = saved.get("offset", 0) if isinstance(saved, dict) else 0
            if (isinstance(saved, dict) and saved.get("identity") == identity
                    and saved.get("size") == stat.st_size
                    and saved.get("mtime_ns") == stat.st_mtime_ns and offset == stat.st_size):
                return
            if saved is None and self.initial and not self.from_start:
                offset = stat.st_size
            elif saved is not None:
                rotated = saved.get("identity") != identity or offset > stat.st_size
                if not rotated and offset and saved.get("anchor"):
                    source.seek(max(0, offset - 128))
                    rotated = source.read(min(128, offset)).hex() != saved["anchor"]
                if rotated:
                    offset = 0
                    assembler.pending = ""
                    self.pending_starts.pop(stream_key, None)
                    self.status["rotations"] += 1
            source.seek(offset)
            for _ in range(1000):
                start = source.tell()
                raw = source.readline(MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE:
                    while raw and not raw.endswith(b"\n"):
                        raw = source.readline(MAX_LINE + 1)
                    self._error("desktop log line exceeded local observation limit")
                    assembler.pending = ""
                    continue
                if not raw.endswith(b"\n"):
                    source.seek(start)
                    break
                chunk = desktop_stderr_chunk(raw.decode("utf-8", errors="replace"))
                if chunk is not None:
                    began_empty = not assembler.pending
                    previous = assembler.errors
                    assembler.feed(chunk)
                    checkpoints = self.pending_starts.get(stream_key, {})
                    if assembler.pending and (began_empty or key not in checkpoints):
                        end = source.tell()
                        source.seek(max(0, start - 128))
                        anchor = source.read(min(128, start)).hex()
                        source.seek(end)
                        self.pending_starts.setdefault(stream_key, {})[key] = (start, identity, anchor)
                    elif not assembler.pending:
                        self.pending_starts.pop(stream_key, None)
                    if assembler.errors != previous:
                        self._error("desktop stderr record was incomplete or malformed")
            offset = source.tell()
            source.seek(max(0, offset - 128))
            anchor = source.read(min(128, offset)).hex()
            self.positions[key] = {"offset": offset, "identity": identity, "anchor": anchor,
                                   "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
