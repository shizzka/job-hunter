"""Structured, per-operation diagnostic traces with bounded local artifacts."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("debug_trace")

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SENSITIVE_KEY_PARTS = ("password", "secret", "api_key", "authorization", "cookie_value", "token")
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]{12,}"),
)
_SAFE_URL_QUERY_KEYS = {"vacancyId", "from", "hhtmFrom", "filter"}


def _safe_name(value: str, fallback: str = "trace") -> str:
    return _SAFE_NAME_RE.sub("_", str(value or "")).strip("._") or fallback


def _redact_text(value: str, *, limit: int | None = 4000) -> str:
    text = str(value)
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text if limit is None else text[:limit]


def _sanitize(value: Any, *, key: str = "") -> Any:
    key_lower = key.casefold()
    if any(part in key_lower for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, str):
        if key_lower == "url" or key_lower.endswith("_url"):
            return safe_page_url(value)
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def safe_page_url(value: str) -> str:
    """Keep useful routing data while dropping arbitrary query secrets."""
    try:
        parsed = urlsplit(str(value or ""))
        safe_query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key in _SAFE_URL_QUERY_KEYS
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query), ""))
    except Exception:
        return ""


def _git_revision(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _private_write_text(path: Path, text: str) -> None:
    encoded = text.encode("utf-8", errors="replace")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sanitize_html(value: str) -> str:
    """Keep DOM structure/selectors while dropping executable and entered data."""
    html = re.sub(
        r"(?is)<script\b[^>]*>.*?</script>",
        "<script data-trace-redacted=\"true\"></script>",
        str(value or ""),
    )
    html = re.sub(
        r"(?is)(<textarea\b[^>]*>).*?(</textarea>)",
        r"\1[REDACTED]\2",
        html,
    )
    html = re.sub(
        r"(?is)(\svalue\s*=\s*)([\"']).*?\2",
        r"\1\2[REDACTED]\2",
        html,
    )
    html = re.sub(
        r"(?i)((?:csrf|xsrf|token|password|authorization|api[_-]?key|cookie)[\w-]*\s*[:=]\s*)([\"']?)[^\s\"'<>]+\2",
        r"\1[REDACTED]",
        html,
    )
    html = re.sub(
        r"(?i)(https?://[^\s\"'<>?]+)\?[^\s\"'<>]+",
        r"\1?[REDACTED]",
        html,
    )
    return _redact_text(html, limit=None)


def cleanup_traces(root: Path, *, retention_days: int, max_runs: int) -> None:
    """Remove only recognized trace directories below ``root``."""
    if not root.is_dir() or root.is_symlink():
        return
    finished: list[Path] = []
    stale_unfinished: list[Path] = []
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=max(1, retention_days))
    for day_dir in root.iterdir():
        if day_dir.is_symlink() or not day_dir.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            continue
        for trace_dir in day_dir.iterdir():
            if trace_dir.is_symlink() or not trace_dir.is_dir():
                continue
            if not (trace_dir / "trace.jsonl").is_file():
                continue
            modified = datetime.fromtimestamp(trace_dir.stat().st_mtime, tz=now.tzinfo)
            if (trace_dir / "summary.txt").is_file():
                finished.append(trace_dir)
            elif modified < cutoff:
                stale_unfinished.append(trace_dir)

    ordered = sorted(finished, key=lambda path: path.stat().st_mtime, reverse=True)
    for index, trace_dir in enumerate(ordered):
        modified = datetime.fromtimestamp(trace_dir.stat().st_mtime, tz=now.tzinfo)
        if modified >= cutoff and index < max(1, max_runs):
            continue
        try:
            shutil.rmtree(trace_dir)
        except OSError as exc:
            log.warning("Could not remove expired trace %s: %s", trace_dir, exc)

    for trace_dir in stale_unfinished:
        try:
            shutil.rmtree(trace_dir)
        except OSError as exc:
            log.warning("Could not remove abandoned trace %s: %s", trace_dir, exc)

    for day_dir in root.iterdir():
        if day_dir.is_dir() and not day_dir.is_symlink():
            try:
                day_dir.rmdir()
            except OSError:
                pass


class ApplyTrace:
    """Append-only trace for one application attempt."""

    def __init__(
        self,
        *,
        trace_id: str,
        trace_dir: Path,
        source: str,
        vacancy_id: str,
        profile: str,
        mode: str,
        started_at: datetime,
        project_root: Path,
    ):
        self.trace_id = trace_id
        self.trace_dir = trace_dir
        self.source = source
        self.vacancy_id = vacancy_id
        self.profile = profile
        self.mode = mode
        self.started_at = started_at
        self._started_monotonic = time.monotonic()
        self._events: list[dict] = []
        self._artifacts: list[str] = []
        self.last_stage = "TRACE_STARTED"
        self.failure_stage = ""
        self.finished = False
        self.revision = _git_revision(project_root)

    @classmethod
    def create(
        cls,
        *,
        home_dir: str,
        source: str,
        vacancy_id: str,
        profile: str,
        mode: str,
        project_root: str | Path | None = None,
        retention_days: int = 14,
        max_runs: int = 100,
    ) -> ApplyTrace:
        started_at = datetime.now().astimezone()
        root = Path(home_dir).expanduser() / "traces"
        day_dir = root / started_at.strftime("%Y-%m-%d")
        os.makedirs(day_dir, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(day_dir, 0o700)

        source_key = _safe_name(source, "source")
        vacancy_key = _safe_name(vacancy_id, "unknown")
        profile_key = _safe_name(profile, "default")
        stamp = started_at.strftime("%Y%m%dT%H%M%S")
        trace_id = f"{source_key}:{vacancy_key}:{profile_key}:{stamp}"
        base_name = f"{source_key}_{vacancy_key}_{started_at.strftime('%H%M%S')}"
        trace_dir = day_dir / base_name
        suffix = 1
        while trace_dir.exists():
            suffix += 1
            trace_dir = day_dir / f"{base_name}_{suffix}"
        if suffix > 1:
            trace_id = f"{trace_id}:{suffix}"
        os.mkdir(trace_dir, 0o700)

        trace = cls(
            trace_id=trace_id,
            trace_dir=trace_dir,
            source=source_key,
            vacancy_id=vacancy_key,
            profile=profile_key,
            mode=mode,
            started_at=started_at,
            project_root=Path(project_root or Path(__file__).resolve().parent),
        )
        trace.event(
            "TRACE_STARTED",
            ok=True,
            source=source_key,
            vacancy_id=vacancy_key,
            profile=profile_key,
            mode=mode,
            revision=trace.revision,
        )
        try:
            cleanup_traces(root, retention_days=retention_days, max_runs=max_runs)
        except Exception as exc:
            log.warning("Trace cleanup failed: %s", type(exc).__name__)
        return trace

    @property
    def jsonl_path(self) -> Path:
        return self.trace_dir / "trace.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.trace_dir / "summary.txt"

    def has_stage(self, stage: str) -> bool:
        return any(event.get("stage") == stage for event in self._events)

    def event(self, stage: str, *, ok: bool | None = None, **fields: Any) -> None:
        try:
            event = {
                "t": round(time.monotonic() - self._started_monotonic, 3),
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "trace_id": self.trace_id,
                "stage": _safe_name(stage, "EVENT").upper(),
            }
            if ok is not None:
                event["ok"] = bool(ok)
            event.update(_sanitize(fields))
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            fd = os.open(self.jsonl_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._events.append(event)
            if event["stage"] != "ARTIFACT_CAPTURED":
                self.last_stage = str(event["stage"])
            status = "" if ok is None else f" ok={bool(ok)}"
            log.info("[TRACE %s] %.3f %s%s", self.trace_id, event["t"], event["stage"], status)
        except Exception as exc:
            log.warning("Trace event write failed at %s: %s", stage, type(exc).__name__)

    async def capture(
        self,
        page,
        name: str,
        *,
        screenshot: bool = True,
        html: bool = True,
    ) -> dict[str, str]:
        artifact_name = _safe_name(name, "artifact")
        saved: dict[str, str] = {}
        try:
            if screenshot:
                path = self.trace_dir / f"{artifact_name}.png"
                previous_umask = os.umask(0o077)
                try:
                    await page.screenshot(path=os.fspath(path))
                finally:
                    os.umask(previous_umask)
                os.chmod(path, 0o600)
                saved["screenshot"] = os.fspath(path)
                self._artifacts.append(path.name)
            if html:
                path = self.trace_dir / f"{artifact_name}.html"
                _private_write_text(path, _sanitize_html(await page.content()))
                saved["html"] = os.fspath(path)
                self._artifacts.append(path.name)
            self.event("ARTIFACT_CAPTURED", ok=True, name=artifact_name, files=list(saved.values()))
        except Exception as exc:
            self.event("ARTIFACT_CAPTURED", ok=False, name=artifact_name, error=type(exc).__name__)
        return saved

    def finish(self, *, ok: bool, message: str = "", failure_stage: str = "") -> None:
        if self.finished:
            return
        previous_stage = failure_stage or self.last_stage
        self.failure_stage = "" if ok else previous_stage
        terminal_stage = "TRACE_COMPLETED" if ok else "TRACE_FAILED"
        self.event(
            terminal_stage,
            ok=ok,
            result_message=message,
            failure_stage="" if ok else previous_stage,
            artifacts=list(dict.fromkeys(self._artifacts)),
        )
        lines = [
            "TRACE RESULT",
            f"Trace ID: {self.trace_id}",
            f"Profile: {self.profile}",
            f"Source: {self.source}",
            f"Vacancy: {self.vacancy_id}",
            f"Mode: {self.mode}",
            f"Revision: {self.revision}",
            f"Result: {'OK' if ok else 'FAIL'}",
        ]
        if not ok:
            lines.append(f"Failure stage: {self.failure_stage}")
        if message:
            lines.append(f"Message: {_redact_text(message)}")
        lines.extend(["", "Stages:"])
        for event in self._events:
            marker = ""
            if "ok" in event:
                marker = " OK" if event["ok"] else " FAIL"
            excluded = {"t", "ts", "trace_id", "stage", "ok", "artifacts"}
            details = []
            for key, value in event.items():
                if key in excluded or value in (None, "", [], {}):
                    continue
                rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                details.append(f"{key}={rendered[:240]}")
            suffix = f" {' '.join(details)}" if details else ""
            lines.append(f"{event['t']:8.3f} {event['stage']}{marker}{suffix}")
        lines.extend(["", "Artifacts:"])
        artifacts = list(dict.fromkeys(self._artifacts))
        lines.extend(f"- {name}" for name in artifacts)
        if not artifacts:
            lines.append("- none")
        try:
            _private_write_text(self.summary_path, "\n".join(lines) + "\n")
        except Exception as exc:
            log.warning("Trace summary write failed: %s", type(exc).__name__)
        self.finished = True
