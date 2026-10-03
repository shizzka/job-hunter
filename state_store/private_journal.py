"""Private append records with cooperative locks and complete short writes."""
import fcntl
import json
import os
import stat
from pathlib import Path

from .json_store import file_lock


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view)
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError("Incomplete private journal write")
        view = view[count:]


def open_private_append(path):
    target = Path(path).absolute()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_RDWR | os.O_APPEND | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(target, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(target, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Private journal must be a regular file")
        os.fchmod(fd, 0o600)
        if created:
            parent_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def append_bytes(path, data, *, separate_tail=False, durable=True, rollback=True):
    target = Path(path).absolute()
    with file_lock(target):
        fd = open_private_append(target)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            prefix = b"\n" if separate_tail and size and os.pread(fd, 1, size-1) != b"\n" else b""
            try:
                write_all(fd, prefix + data)
            except BaseException:
                # Only our partial append is removed, while both locks are held.
                if rollback:
                    os.ftruncate(fd, size)
                    os.fsync(fd)
                raise
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)


def append_json(path, record, *, durable=True, default=None):
    # Serialization must finish before creating/modifying the journal or tail.
    line = json.dumps(record, ensure_ascii=False, default=default, allow_nan=False) + "\n"
    append_bytes(path, line.encode("utf-8"), separate_tail=True, durable=durable)


def append_text(path, text, *, durable=False):
    # Raw inherited stdout writers may not cooperate; never truncate their bytes.
    append_bytes(path, text.encode("utf-8", errors="replace"), durable=durable, rollback=False)


def read_json_records(path, *, strip_nuls=False):
    """Read one locked inode; retain, but skip malformed diagnostic records."""
    try:
        stream = open(path, "rb")
    except FileNotFoundError:
        return []
    with stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        records = []
        for line in stream:
            if strip_nuls:
                line = line.replace(b"\x00", b"")
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
