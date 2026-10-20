"""Read-only direction scopes for Telegram response views."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import chat_context
import directions


class DirectionView:
    def __init__(self, home):
        self.home = home
        self.active = directions.active_direction(home) or {}
        self.directions = directions.list_directions(home)
        self.name = self.active.get("name", "")
        self.title = self.active.get("title", "")
        self.callback_scope = hashlib.sha256(self.name.encode()).hexdigest()[:12] if self.name else ""

    def item_direction(self, item):
        """Use recorded evidence, never vacancy titles or the current selection."""
        vacancy = item.get("vacancy") or {}
        selection = vacancy.get("_direction_selection") or {}
        names = {d["name"] for d in self.directions}
        if selection.get("direction"):
            return selection["direction"] if selection["direction"] in names else None
        evidence = []
        query = " ".join(str(vacancy.get("_search_query") or "").casefold().split())
        if query:
            evidence.append({d["name"] for d in self.directions if query in {
                " ".join(q.casefold().split()) for q in d["search_queries"]}})
        for key in ("hh_resume_hash", "hh_resume_numeric_id"):
            identity = selection.get(key) or vacancy.get(key)
            if identity:
                evidence.append({d["name"] for d in self.directions for v in d["variants"]
                                 if str(v.get(key) or "") == str(identity)})
        for version in (vacancy.get("_resume_input_versions") or {}).values():
            digest = version.get("sha256")
            if digest:
                evidence.append({d["name"] for d in self.directions for v in d["variants"]
                                 if v.get("text_sha256") == digest})
        if evidence:
            matches = set.intersection(*evidence)
            return next(iter(matches)) if len(matches) == 1 else None
        # An old profile with a single direction has no competing owner.
        if len(names) == 1:
            return next(iter(names))
        return "" if not names else None

    def review_items(self, items, *, unspecified=False):
        return [item for item in items if self.item_direction(item) == (None if unspecified else self.name)]

    def chat_status(self, chat_id):
        owner = chat_context.safe_resolve(self.home, str(chat_id or ""))
        if owner.status == chat_context.OWN:
            return "selected" if owner.direction == self.name else "other"
        return "other" if owner.status == chat_context.OTHER else "review"


def response_counts(home):
    """Latest complete pass per filter; partial passes are explicit lower bounds."""
    records, summaries = {}, {"active": [], "archived": []}
    try:
        with (Path(home) / "negotiation_snapshots.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("filter") not in summaries or not row.get("snapshot_id"):
                    continue
                key = (row["filter"], row["snapshot_id"])
                if row.get("kind") == "snapshot_summary":
                    summaries[row["filter"]].append(row)
                elif row.get("id") is not None:
                    records.setdefault(key, {})[str(row["id"])] = row
    except FileNotFoundError:
        pass
    mapping = chat_context.profile_resumes(home)
    view = DirectionView(home)
    result = {}
    for section, passes in summaries.items():
        complete = [s for s in passes if s.get("complete") is True
                    and len(records.get((section, s["snapshot_id"]), {})) == s.get("total")]
        chosen = (complete or [s for s in passes if s.get("pages_read", 0) > 0])
        if not chosen:
            result[section] = None
            continue
        latest = max(chosen, key=lambda s: s.get("fetched_at", ""))
        rows = records.get((section, latest["snapshot_id"]), {}).values()
        counts = {"selected": 0, "unmapped": 0, "complete": latest in complete,
                  "fetched_at": latest.get("fetched_at", ""),
                  "newer_partial": any(s.get("fetched_at", "") > latest.get("fetched_at", "")
                                       and s.get("complete") is not True for s in passes)}
        for row in rows:
            direction = mapping.get(str(row.get("resumeId") or ""), (None,))[0]
            if direction is None or (view.directions and not direction):
                counts["unmapped"] += 1
            elif direction == view.name:
                counts["selected"] += 1
        result[section] = counts
    return result
