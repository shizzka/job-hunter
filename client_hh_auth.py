"""HH auth and resume import flow for client profiles."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path

import config
import profile as profile_mod
from hh_client import HHClient


def _resolve_profile(profile_name: str):
    try:
        return profile_mod.load_profile(profile_name)
    except FileNotFoundError:
        active = profile_mod.load_profile()
        active_home = os.path.normpath(active.home_dir)
        if active_home.endswith(os.path.join("profiles", profile_name)) or os.path.basename(active_home) == profile_name:
            active.name = profile_name
            return active
        raise


def hh_resume_catalog_path(profile_name: str) -> str:
    profile = _resolve_profile(profile_name)
    return os.path.join(profile.home_dir, "hh_resumes.json")


def hh_resume_exports_dir(profile_name: str) -> str:
    profile = _resolve_profile(profile_name)
    return os.path.join(profile.home_dir, "hh_resumes")


def load_hh_resume_catalog(profile_name: str) -> list[dict]:
    path = hh_resume_catalog_path(profile_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "").strip()).strip("_")
    return slug or "resume"


def _write_text(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    return path


def _normalize_env_value(value: str | int | None) -> str:
    raw = "" if value is None else str(value)
    return re.sub(r"\s+", " ", raw).strip()


def _save_resume_catalog(profile_name: str, items: list[dict]) -> str:
    path = hh_resume_catalog_path(profile_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return path


def _update_profile_resume_ids(profile_name: str, resumes: list[dict]) -> str:
    profile = _resolve_profile(profile_name)
    env_file = os.path.join(profile.home_dir, "profile.env")
    if not os.path.isfile(env_file):
        raise FileNotFoundError(f"Профиль '{profile_name}' не найден: {env_file}")

    updates: dict[str, str] = {}
    slots = (
        ("HH_PRIMARY_RESUME_ID", "HH_PRIMARY_RESUME_TITLE"),
        ("HH_SECONDARY_RESUME_ID", "HH_SECONDARY_RESUME_TITLE"),
        ("HH_TERTIARY_RESUME_ID", "HH_TERTIARY_RESUME_TITLE"),
    )
    for idx, (id_key, title_key) in enumerate(slots):
        item = resumes[idx] if idx < len(resumes) else {"id": "", "title": ""}
        updates[id_key] = _normalize_env_value(item.get("id") or "")
        updates[title_key] = _normalize_env_value(item.get("title") or "")

    with open(env_file, encoding="utf-8") as f:
        lines = f.read().splitlines()

    key_to_index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        key_to_index[key.strip()] = idx

    for key, value in updates.items():
        rendered = f"{key}={_normalize_env_value(value)}"
        if key in key_to_index:
            lines[key_to_index[key]] = rendered
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)

    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    return env_file


async def import_current_hh_resumes(client: HHClient, profile_name: str) -> dict:
    profile = _resolve_profile(profile_name)
    resumes = await client.get_resume_ids()
    exports: list[dict] = []
    exports_dir = hh_resume_exports_dir(profile_name)
    os.makedirs(exports_dir, exist_ok=True)

    for item in resumes:
        downloaded = await client.download_resume_by_id(item)
        resume_id = str(item.get("id") or "")
        title = str(item.get("title") or resume_id or "resume")
        filename = f"{resume_id or _slugify(title)}.md"
        path = os.path.join(exports_dir, filename)
        _write_text(path, downloaded.get("raw", ""))
        exports.append(
            {
                "id": resume_id,
                "title": title,
                "url": str(item.get("url") or ""),
                "path": path,
                "sections": downloaded.get("sections", []),
            }
        )

    catalog_path = _save_resume_catalog(profile_name, exports)
    profile_env_path = _update_profile_resume_ids(profile_name, exports) if exports else ""

    selected = exports[0] if exports else None
    resume_file = ""
    if selected and selected.get("path"):
        raw = Path(selected["path"]).read_text(encoding="utf-8")
        resume_file = _write_text(profile.resume_file, raw)

    return {
        "ok": bool(exports),
        "count": len(exports),
        "catalog_path": catalog_path,
        "profile_env_path": profile_env_path,
        "resume_file": resume_file,
        "resumes": exports,
    }


async def run_hh_auth_capture(profile_name: str, *, timeout_sec: int = 900, poll_sec: int = 3, activate_profile: bool = True) -> dict:
    target_profile = _resolve_profile(profile_name)
    if activate_profile:
        profile_mod.activate_no_lock(profile_name)
    client = HHClient()
    try:
        await client.start(headless=False)
        await client._page.goto(  # noqa: SLF001 - reusing existing HH client page flow
            f"{config.HH_BASE_URL}/account/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        elapsed = 0
        while elapsed <= max(1, timeout_sec):
            if client._page.is_closed():  # noqa: SLF001 - auth flow owns the page lifecycle
                return {
                    "ok": False,
                    "authenticated": False,
                    "timeout": False,
                    "profile_name": profile_name,
                    "cookies_file": target_profile.hh.cookies_file,
                    "count": 0,
                    "resumes": [],
                    "error": "Окно HH auth было закрыто до завершения входа.",
                }
            if await client.is_logged_in_passive():
                await client.save_session()
                imported = await import_current_hh_resumes(client, profile_name)
                return {
                    "ok": imported.get("ok", False),
                    "authenticated": True,
                    "timeout": False,
                    "profile_name": profile_name,
                    "cookies_file": target_profile.hh.cookies_file,
                    **imported,
                }
            await asyncio.sleep(poll_sec)
            elapsed += poll_sec

        return {
            "ok": False,
            "authenticated": False,
            "timeout": True,
            "profile_name": profile_name,
            "cookies_file": target_profile.hh.cookies_file,
            "count": 0,
            "resumes": [],
        }
    finally:
        await client.stop()


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="HH auth capture for client profile")
    parser.add_argument("--profile", required=True, help="Client profile name")
    parser.add_argument("--timeout", type=int, default=900, help="HH auth timeout in seconds")
    args = parser.parse_args()

    try:
        result = await run_hh_auth_capture(args.profile, timeout_sec=args.timeout, activate_profile=True)
    except Exception as exc:
        result = {
            "ok": False,
            "authenticated": False,
            "timeout": False,
            "profile_name": args.profile,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
