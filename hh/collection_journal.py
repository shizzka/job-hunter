"""Append observations to the journal inode, without creating state sidecars."""
import fcntl
import json
import os

from state_store.private_journal import open_private_append, write_all
from state_store.locking import acquire_flock


def append_records(path, records):
    data = ''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'
                   for row in records).encode('utf-8')
    if not data:
        return
    fd = open_private_append(path)
    try:
        acquire_flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        # Preserve even an interrupted previous append; never truncate history.
        prefix = b'\n' if size and os.pread(fd, 1, size - 1) != b'\n' else b''
        write_all(fd, prefix + data)
        os.fsync(fd)
    finally:
        os.close(fd)
