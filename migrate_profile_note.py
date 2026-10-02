"""Copy a legacy global candidate note into one explicitly selected profile."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from profile import _normalize_env_value, _parse_env_file


def migrate_profile_note(env_file: str, profile_dir: str, *, include_salary: bool = False) -> Path:
    directory = Path(profile_dir).resolve()
    if not (directory / "profile.env").is_file():
        raise ValueError("Target must be an existing named profile with profile.env")
    values = _parse_env_file(env_file)
    note = values.get("HH_AUTO_ANSWER_PROFILE_NOTE", "").strip()
    if not note:
        raise ValueError("Source has no HH_AUTO_ANSWER_PROFILE_NOTE")
    knowledge = directory / "knowledge"
    if knowledge.is_symlink():
        raise ValueError("Refusing a symlinked knowledge directory")
    knowledge.mkdir(mode=0o700, exist_ok=True)
    target = knowledge / "profile_note.md"
    content = note + "\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if target.is_symlink() or target.read_text(encoding="utf-8") != content:
            raise ValueError("Target already exists; refusing to overwrite candidate data") from None
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    if include_salary:
        profile_env = directory / "profile.env"
        with profile_env.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            own_values = _parse_env_file(str(profile_env))
            additions = {
                key: value for key, value in values.items()
                if key.startswith("HH_AUTO_ANSWER_SALARY_") and value and key not in own_values
            }
            if additions:
                stream.write("\n# Candidate salary settings migrated from legacy global config\n")
                for key, value in additions.items():
                    stream.write(f"{key}={_normalize_env_value(value)}\n")
                stream.flush()
                os.fsync(stream.fileno())
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--include-salary", action="store_true", help="Copy missing salary settings for the same owner; keep existing profile values")
    args = parser.parse_args()
    try:
        target = migrate_profile_note(args.env_file, args.profile_dir, include_salary=args.include_salary)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Migration failed: {exc}\n")
    print(f"Candidate note preserved at {target}; source env unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
