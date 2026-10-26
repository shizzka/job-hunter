"""Bounded read-only report caches and incremental compact journal projections.

Only complete JSONL lines are committed. Rotation, truncation and same-size
rewrites invalidate the projection. Large prompts/HTML never live in the index.
"""
from collections import OrderedDict
import copy
import fcntl
import json
import os
from pathlib import Path
import threading
import time

from state_store.locking import acquire_flock

_FIELDS = set('''event created_at run_id vacancy_id application_id source decision outcome
status status_bucket status_detail_bucket direction direction_title hh_resume_hash
hh_resume_numeric_id requested_resume_id resume_id resumeId resume_variant variant_text_sha256 text_sha256
matcher_resume_sha256 cover_letter_resume_sha256 _resume_input_versions _direction_selection
search_query _search_query query reason_code reason_group note score cluster cover_style
retry_reason retry_outcome funnel applied mode dry_run title company fetched_at kind vacancyId
questions_count best_guess_count required_count success
observed_at_utc recorded_at_utc url historical screening_type submission_mode cover_letter_status verdict
failure_id'''.split())
_IDENTITY = set('''direction hh_resume_hash hh_resume_numeric_id requested_resume_id resume_id
resumeId variant_text_sha256 text_sha256 matcher_resume_sha256 cover_letter_resume_sha256
_resume_input_versions _direction_selection search_query _search_query query'''.split())
_lock = threading.RLock()
_journals = OrderedDict()
_reports = OrderedDict()
# About two 110k-row journals. Set to zero to disable journal retention.
MAX_CACHED_ROWS = max(0, int(os.environ.get("ANALYTICS_CACHE_MAX_ROWS", "250000")))
IDLE_SECONDS = 30 * 60


def _prune(now):
    """Called under _lock; eviction drops cache ownership, never reader snapshots."""
    for key, item in list(_journals.items()):
        if now - item.last_used >= IDLE_SECONDS:
            del _journals[key]
    rows = sum(len(item.rows) + len(item.tail) for item in _journals.values())
    while _journals and (rows > MAX_CACHED_ROWS or len(_journals) > 8):
        _, item = _journals.popitem(last=False)
        rows -= len(item.rows) + len(item.tail)
    for key, entry in list(_reports.items()):
        if now - entry[4] >= IDLE_SECONDS:
            del _reports[key]


def stamp(path):
    try:
        stat = os.stat(path)
        return (os.fspath(Path(path).absolute()), stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        return (os.fspath(Path(path).absolute()), None)


def ownership_key(view):
    # HistoryScope has already validated the caller's direction access.
    return (str(view.home.absolute()), view.name,
            json.dumps(view.directions, ensure_ascii=False, sort_keys=True),
            stamp(view.home / 'negotiation_snapshots.jsonl'))


def project(row):
    if row.get("event") in {"search_started", "search_finished", "stage_failed", "failure_observed",
                            "debug_artifact", "hh_ui_isolation"}:
        return dict(row)
    result = {k: v for k, v in row.items() if k in _FIELDS}
    if isinstance(row.get('vacancy'), dict):
        result['vacancy'] = {k: v for k, v in row['vacancy'].items() if k in _IDENTITY}
    return result


class Journal:
    def __init__(self, projection=project, line_filter=None):
        self.projection = projection
        self.line_filter = line_filter
        self.tail = []
        self.lock = threading.RLock()
        self.version = None
        self.offset = 0
        self.rows = []
        self.bytes_read = 0
        self.last_used = time.monotonic()
        self.cache_key = None

    def read(self, path):
        result = self._read(path)
        with _lock:
            now = time.monotonic()
            # A concurrent read may already have evicted this item. Do not
            # resurrect it or replace a newer journal for the same path.
            if _journals.get(self.cache_key) is self:
                self.last_used = now
                _journals.move_to_end(self.cache_key)
            _prune(now)
        return result

    def _read(self, path):
        with self.lock:
            current = stamp(path)
            if current == self.version:
                return self.rows + self.tail, current
            if current[1] is None:
                self.rows, self.offset, self.version, self.tail = [], 0, current, []
                return self.rows + self.tail, current
            with open(path, 'rb') as stream:
                acquire_flock(stream.fileno(), fcntl.LOCK_SH)
                stat = os.fstat(stream.fileno())
                current = (os.fspath(Path(path).absolute()), stat.st_dev, stat.st_ino,
                           stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                append = (self.version and self.version[1:3] == current[1:3]
                          and current[3] > self.version[3])
                if not append:
                    self.rows, self.offset = [], 0
                stream.seek(self.offset)
                additions = []
                self.tail = []
                while True:
                    line = stream.readline()
                    self.bytes_read += len(line)
                    if not line:
                        break
                    complete = line.endswith(b'\n')
                    if complete:
                        self.offset = stream.tell()
                    if self.line_filter is not None and not self.line_filter(line):
                        if not complete:
                            break
                        continue
                    try:
                        row = json.loads(line.replace(b'\x00', b''))
                    except (ValueError, UnicodeError):
                        if not complete:
                            break
                        continue
                    if isinstance(row, dict):
                        (additions if complete else self.tail).append(self.projection(row))
                    if not complete:
                        break
                # Readers keep an immutable list snapshot while another thread appends.
                self.rows = self.rows + additions
                self.version = current
            return self.rows + self.tail, current


def journal(path, *, tag="summary", projection=project, line_filter=None):
    key = (os.fspath(Path(path).absolute()), tag)
    with _lock:
        now = time.monotonic()
        _prune(now)
        item = _journals.pop(key, None) or Journal(projection, line_filter)
        item.cache_key = key
        item.last_used = now
        _journals[key] = item
        _prune(now)
    return item


def memo(key, compute, *, now=None):
    """Coalesce one report key, without serializing unrelated files/users."""
    with _lock:
        touched = time.monotonic()
        _prune(touched)
        entry = _reports.pop(key, None)
        if entry is None:
            entry = [threading.RLock(), None, None, False, touched]
        entry[4] = touched
        _reports[key] = entry
        while len(_reports) > 32:
            _reports.popitem(last=False)
    with entry[0]:
        current = time.time() if now is None else now
        if not entry[3] or entry[2] is not None and current >= entry[2]:
            value, expiry = compute()
            entry[1:4] = [value, expiry, True]
        return copy.deepcopy(entry[1])
