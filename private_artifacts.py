"""Bound, private diagnostic images/HTML; no process-wide umask over awaits."""
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from state_store.json_store import atomic_write_bytes, atomic_write_text

log = logging.getLogger(__name__)


def state_dir_for(client, fallback):
    paths = getattr(client, "_cookie_paths", None)
    if paths is not None:
        return paths.state_dir
    session = getattr(client, "_cookie_session", None)
    return str(session.state_dir) if session is not None else os.path.abspath(fallback)


async def private_screenshot(page, path, **options):
    target = Path(path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory deletes only its new, owned 0700 directory.
    with tempfile.TemporaryDirectory(prefix=".screenshot-", dir=target.parent, ignore_cleanup_errors=True) as temporary:
        captured = Path(temporary) / "capture.png"
        await page.screenshot(path=str(captured), **options)
        captured.chmod(0o600)
        atomic_write_bytes(target, captured.read_bytes())


async def capture_artifacts(page, root, label, *, screenshot=True, html=True, full_page=False):
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label)).strip("._")[:100] or "snapshot"
    directory = Path(tempfile.mkdtemp(prefix=label + "-", dir=root))
    saved = {}
    completed = False
    try:
        if screenshot:
            path = directory / (label + ".png")
            try:
                await private_screenshot(page, path, **({"full_page": True} if full_page else {}))
                saved["screenshot"] = str(path)
            except Exception as exc:
                log.debug("Diagnostic image capture failed: %s", type(exc).__name__)
        if html:
            path = directory / (label + ".html")
            try:
                from debug_trace import _sanitize_html
                content = _sanitize_html(await page.content())
                atomic_write_text(path, content)
                saved["html"] = str(path)
            except Exception as exc:
                log.debug("Diagnostic HTML capture failed: %s", type(exc).__name__)
        completed = True
        return saved
    finally:
        if not completed or not saved:
            # Only the exact directory created above is eligible for cleanup.
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                log.debug("Diagnostic cleanup failed: %s", type(exc).__name__)
