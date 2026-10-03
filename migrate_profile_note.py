"""Copy a legacy global candidate note into one explicitly selected profile."""
from __future__ import annotations

import argparse
from pathlib import Path

from profile import _parse_env_file, update_env_file
from state_store.json_store import atomic_create_text


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
        atomic_create_text(target, content)
    except FileExistsError:
        if target.is_symlink() or target.read_text(encoding="utf-8") != content:
            raise ValueError("Target already exists; refusing to overwrite candidate data") from None
    if include_salary:
        profile_env = directory / "profile.env"
        additions = {
            key: value for key, value in values.items()
            if key.startswith("HH_AUTO_ANSWER_SALARY_") and value
        }
        update_env_file(
            profile_env, additions, only_missing=True,
            comment="Candidate salary settings migrated from legacy global config",
        )
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
