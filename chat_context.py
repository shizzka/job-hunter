"""Which profile and direction an hh chat belongs to (BACKLOG B13).

Several profiles may share one hh account (qa and electrician do), so every
profile sees every chat. A reply must use the facts of the resume the
application was sent with, otherwise an electrician profile answers a QA
employer with an electrician's salary and experience (dev 19.10).

chat id -> resumeId comes from the profile's negotiation snapshot; resumeId ->
direction from the profile's direction variants, or from its legacy
HH_*_RESUME_ID hashes mapped to numeric ids through any direction variant on
this machine (the same hh account).
"""
from __future__ import annotations

import glob
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

OWN = "own"          # the resume belongs to this profile (direction may be set)
OTHER = "other"      # the resume belongs to another profile / is not this profile's
UNKNOWN = "unknown"  # no snapshot record for this chat


@dataclass(frozen=True)
class ChatOwner:
    status: str
    resume_id: str = ""
    direction: str = ""         # direction name inside this profile, "" for a legacy profile
    title: str = ""             # resume title
    owner_profile: str = ""     # for OTHER: the profile that owns the resume, when known


def chat_resume_id(home: str, chat_id: str) -> str:
    resume = ""
    try:
        with open(os.path.join(home, "negotiation_snapshots.jsonl"), encoding="utf-8") as stream:
            for line in stream:
                if '"chatId"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("chatId") or "") == str(chat_id) and row.get("resumeId"):
                    resume = str(row["resumeId"])
    except OSError:
        return ""
    return resume


def _directions(home: str) -> list[dict]:
    result = []
    for path in sorted(glob.glob(os.path.join(home, "directions", "*.json"))):
        try:
            with open(path, encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("name") and isinstance(value.get("variants"), list):
            result.append(value)
    return result


def _legacy_hashes(home: str) -> list[tuple[str, str]]:
    found = []
    try:
        with open(os.path.join(home, "profile.env"), encoding="utf-8") as stream:
            text = stream.read()
    except OSError:
        return found
    for slot in ("PRIMARY", "SECONDARY", "TERTIARY"):
        hash_match = re.search(rf"^HH_{slot}_RESUME_ID=([0-9a-f]{{38}})\s*$", text, re.M)
        title_match = re.search(rf"^HH_{slot}_RESUME_TITLE=(.*)$", text, re.M)
        if hash_match:
            found.append((hash_match.group(1), (title_match.group(1).strip() if title_match else "")))
    return found


def _hash_to_numeric(profiles_root: str) -> dict[str, tuple[str, str]]:
    mapping = {}
    for home in glob.glob(os.path.join(profiles_root, "*")):
        for direction in _directions(home):
            for variant in direction["variants"]:
                if variant.get("hh_resume_hash") and variant.get("hh_resume_numeric_id"):
                    mapping[str(variant["hh_resume_hash"])] = (str(variant["hh_resume_numeric_id"]),
                                                              str(variant.get("title") or ""))
    return mapping


def profile_resumes(home: str) -> dict[str, tuple[str, str]]:
    """numeric resume id -> (direction name or "", title) for this profile."""
    own = {}
    for direction in _directions(home):
        for variant in direction["variants"]:
            if variant.get("hh_resume_numeric_id"):
                own[str(variant["hh_resume_numeric_id"])] = (direction["name"], str(variant.get("title") or ""))
    numeric = _hash_to_numeric(os.path.dirname(os.path.abspath(home)))
    for resume_hash, title in _legacy_hashes(home):
        if resume_hash in numeric and numeric[resume_hash][0] not in own:
            own[numeric[resume_hash][0]] = ("", title or numeric[resume_hash][1])
    return own


def resolve(home: str, chat_id: str) -> ChatOwner:
    resume = chat_resume_id(home, chat_id)
    if not resume:
        return ChatOwner(UNKNOWN)
    own = profile_resumes(home)
    if resume in own:
        direction, title = own[resume]
        return ChatOwner(OWN, resume, direction, title)
    root = os.path.dirname(os.path.abspath(home))
    # Real profiles first: dev copies (qa-dev, marina-dev) share resumes with them.
    for other_home in sorted(glob.glob(os.path.join(root, "*")), key=lambda p: (p.endswith("-dev"), p)):
        if os.path.abspath(other_home) == os.path.abspath(home):
            continue
        theirs = profile_resumes(other_home)
        if resume in theirs:
            return ChatOwner(OTHER, resume, "", theirs[resume][1], os.path.basename(other_home))
    return ChatOwner(OTHER, resume)


def refusal_text(owner: ChatOwner, profile_name: str) -> str:
    resume = f"резюме «{owner.title}»" if owner.title else f"резюме {owner.resume_id}"
    where = f" — ответьте из профиля {owner.owner_profile}" if owner.owner_profile else ""
    return (f"Этот отклик был отправлен {resume}, а оно не относится к профилю {profile_name}{where}. "
            "Ответ не готовлю: в него попали бы факты другого резюме.")


UNKNOWN_TEXT = ("Не удалось определить, каким резюме отправлен отклик. "
                "Ответ и анкету не готовлю, чтобы не использовать сведения другого резюме. "
                "Проверьте отклик в чате и выберите его профиль и направление.")
DIRECTION_TEXT = ("Отклик отправлен резюме другого направления. "
                  "Выберите направление этого резюме и повторите.")


def safe_resolve(home: str, chat_id: str) -> ChatOwner:
    try:
        return resolve(home, str(chat_id))
    except Exception as exc:
        logging.getLogger(__name__).warning("chat owner lookup failed: %s", type(exc).__name__)
        return ChatOwner(UNKNOWN)


async def resolve_with_refresh(home: str, chat_id: str, cookies_file: str) -> ChatOwner:
    """One metadata refresh on missing/failed ownership; never infer from login."""
    owner = safe_resolve(home, chat_id)
    if owner.status == UNKNOWN or (owner.status == OWN and not owner.resume_id):
        from hh import negotiation_snapshot
        try:
            await asyncio.to_thread(negotiation_snapshot.collect, home_dir=home, cookies_file=cookies_file)
        except Exception as exc:
            logging.getLogger(__name__).warning("chat owner refresh failed: %s", type(exc).__name__)
        owner = safe_resolve(home, chat_id)
    return owner


@dataclass(frozen=True)
class ChatBinding:
    """Application resume authorized for one captured candidate's facts."""
    home: str
    chat_id: str
    resume_id: str
    direction: str

    def check(self, snapshot) -> None:
        owner = safe_resolve(self.home, self.chat_id)
        if (snapshot.profile_home != self.home or snapshot.direction_name != self.direction
                or owner.status != OWN or owner.resume_id != self.resume_id
                or owner.direction != self.direction):
            raise RuntimeError("Chat application ownership changed")


async def bind_candidate(chat_id: str, snapshot, *, home: str, profile_name: str):
    """Return (binding, refusal) before any use of candidate facts for a thread."""
    if not snapshot.profile_home or os.path.abspath(home) != snapshot.profile_home:
        return None, {"ok": False, "reason": "unknown_owner", "chat_id": chat_id,
                      "message": UNKNOWN_TEXT}
    owner = await resolve_with_refresh(snapshot.profile_home, chat_id, snapshot.cookies_path)
    reason, message = "", ""
    if owner.status == OTHER:
        reason, message = "other_profile", refusal_text(owner, profile_name)
    elif owner.status != OWN or not owner.resume_id:
        reason, message = "unknown_owner", UNKNOWN_TEXT
    elif owner.direction != snapshot.direction_name:
        reason, message = "direction_mismatch", DIRECTION_TEXT
    if reason:
        return None, {"ok": False, "reason": reason, "chat_id": chat_id, "message": message}
    return ChatBinding(snapshot.profile_home, str(chat_id), owner.resume_id, owner.direction), None
