"""Runtime settings captured after the active profile is selected."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Immutable path snapshot for one runtime operation."""

    home_dir: str
    hh_state_dir: str
    resume_file: str

    @classmethod
    def from_config(cls, settings: Any) -> RuntimePaths:
        return cls(
            home_dir=os.fspath(settings.JOB_HUNTER_HOME),
            hh_state_dir=os.fspath(settings.HH_STATE_DIR),
            resume_file=os.fspath(settings.RESUME_FILE),
        )
