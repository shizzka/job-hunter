"""Read-only ownership of historical records, using saved evidence only.

No profession guessing and no attribution to the currently selected direction.
The active direction is a display filter, never migration evidence.
"""
from pathlib import Path

import directions
import profile_access

ALL = "*"
UNSPECIFIED = "?"


def vacancy_key(record, field="vacancy_id"):
    source = record.get("source") or "hh"
    return source, str(record.get(field) or "").removeprefix(source + ":")


def runtime_metadata(vacancy=None):
    """Capture requested local identity, including decisions before the matcher."""
    import config
    vacancy = vacancy or {}
    selected = vacancy.get("_direction_selection") or {}
    direction = getattr(config, "ACTIVE_DIRECTION", None) or {}
    variant = selected
    if direction and not selected and vacancy.get("id"):
        try:
            variant = directions.select_variant(direction, str(vacancy.get("id") or ""),
                "\n".join(str(vacancy.get(k) or "") for k in ("title", "snippet", "details")))
        except (ValueError, OSError):
            variant = {}
    return {"direction": selected.get("direction", direction.get("name")),
            "direction_title": selected.get("direction_title", direction.get("title", "")),
            "hh_resume_hash": variant.get("hh_resume_hash") or (
                "" if direction else (vacancy.get("_requested_resume") or {}).get("id")
                or vacancy.get("_hh_resume_id") or getattr(config, "HH_PRIMARY_RESUME_ID", "")),
            "hh_resume_numeric_id": variant.get("hh_resume_numeric_id"),
            "variant_text_sha256": variant.get("text_sha256")}


class HistoryScope:
    def __init__(self, home, scope="active"):
        self.home = Path(home)
        self.directions = directions.list_directions(home)
        active = directions.active_direction(home) or {}
        default = active.get("name") or (self.directions[0]["name"] if len(self.directions) == 1 else
                                         UNSPECIFIED if self.directions else "")
        self.name = default if scope == "active" else scope
        profile_access.require_direction(self.name)
        self.title = ("все направления" if self.name == ALL else "направление не указано"
                      if self.name in {UNSPECIFIED, "", None} else
                      next((d["title"] for d in self.directions if d["name"] == self.name), self.name))

    def owner(self, record):
        vacancy = record.get("vacancy") or {}
        selected = vacancy.get("_direction_selection") or record.get("_direction_selection") or {}
        sources = [selected, record, vacancy]
        names = {d["name"] for d in self.directions}
        explicit = {s["direction"] for s in sources if s.get("direction")}
        if explicit and (len(explicit) != 1 or not explicit <= names):
            return None
        evidence = [explicit] if explicit else []
        for source in sources:
            for field in ("hh_resume_hash", "requested_resume_id", "resume_id", "resumeId", "hh_resume_numeric_id"):
                identity = source.get(field)
                if identity:
                    matches = {d["name"] for d in self.directions for v in d["variants"]
                               if str(identity) in {str(v.get("hh_resume_hash") or ""),
                                                    str(v.get("hh_resume_numeric_id") or "")}}
                    if matches:
                        evidence.append(matches)
            digests = [source.get(k) for k in ("variant_text_sha256", "text_sha256", "matcher_resume_sha256", "cover_letter_resume_sha256")]
            digests += [v.get("sha256") for v in (source.get("_resume_input_versions") or {}).values()]
            for digest in filter(None, digests):
                matches = {d["name"] for d in self.directions for v in d["variants"] if v.get("text_sha256") == digest}
                if matches:
                    evidence.append(matches)
        if not names and not evidence:
            return ""
        if not evidence:
            query = next((s.get(k) for s in sources for k in ("search_query", "_search_query", "query") if s.get(k)), "")
            query = " ".join(str(query).casefold().split())
            if query:
                evidence.append({d["name"] for d in self.directions if query in {
                    " ".join(q.casefold().split()) for q in d["search_queries"]}})
        if evidence:
            matches = set.intersection(*evidence)
            return next(iter(matches)) if len(matches) == 1 else None
        return "" if not names else None

    def matches(self, record):
        owner = self.owner(record)
        return self.name == ALL or owner == (None if self.name == UNSPECIFIED else self.name)

    def metadata(self, record):
        """Preserve a legacy resume id/hash; fill only uniquely known identities."""
        vacancy = record.get("vacancy") or {}
        selected = vacancy.get("_direction_selection") or record.get("_direction_selection") or {}
        sources = [selected, record, vacancy]
        owner = self.owner(record)
        base = {"direction": owner,
                "direction_title": next((d["title"] for d in self.directions if d["name"] == owner), "")}
        hashes = {str(s[k]) for s in sources for k in ("hh_resume_hash", "requested_resume_id", "resume_id") if s.get(k)}
        numbers = {str(s[k]) for s in sources for k in ("hh_resume_numeric_id", "resumeId") if s.get(k)}
        digests = {s[k] for s in sources for k in ("variant_text_sha256", "text_sha256", "matcher_resume_sha256", "cover_letter_resume_sha256") if s.get(k)}
        digests.update(v.get("sha256") for s in sources for v in (s.get("_resume_input_versions") or {}).values())
        identities = hashes | numbers
        variants = [v for d in self.directions if d["name"] == owner for v in d["variants"]
                    if v["hh_resume_hash"] in identities or str(v.get("hh_resume_numeric_id")) in identities
                    or v.get("text_sha256") in digests]
        if len(variants) == 1:
            return {**base, "hh_resume_hash": variants[0]["hh_resume_hash"],
                    "hh_resume_numeric_id": variants[0].get("hh_resume_numeric_id")}
        return {**base, "hh_resume_hash": next(iter(hashes)) if len(hashes) == 1 else "",
                "hh_resume_numeric_id": next(iter(numbers)) if len(numbers) == 1 else None}

    def records(self, records):
        return [r for r in records if self.matches(r)]

    def events(self, records):
        """Recover lifecycle/status ownership only from an unambiguous join."""
        records = [dict(r) for r in records]
        run_owners, lifecycle_owners, vacancy_owners, application_resumes = {}, {}, {}, {}
        for r in records:
            owner = self.owner(r)
            if r.get("run_id") and (r.get("event") == "decision" or
                    r.get("event") in {"search_started", "search_finished"} and owner is not None):
                run_owners.setdefault(r["run_id"], set()).add(owner)
            if r.get("run_id") and r.get("event") in {"search_started", "search_finished"} and owner is not None:
                lifecycle_owners.setdefault(r["run_id"], set()).add(owner)
            if r.get("event") == "decision" and r.get("decision") == "applied_auto":
                key = vacancy_key(r)
                vacancy_owners.setdefault(key, set()).add(owner)
                metadata = self.metadata(r)
                if metadata["hh_resume_hash"] or metadata["hh_resume_numeric_id"]:
                    application_resumes.setdefault(key, set()).add((metadata["hh_resume_hash"], metadata["hh_resume_numeric_id"]))
        for run, owners in lifecycle_owners.items():
            if len(owners) == 1 and (run_owners.get(run, set()) - {None}) <= owners:
                run_owners[run] = owners
        # A saved application resume is stronger evidence than the active search.
        from state_store.private_journal import read_json_records
        try:
            snapshots = read_json_records(self.home / "negotiation_snapshots.jsonl")
        except FileNotFoundError:
            snapshots = []
        snapshot_owners, snapshot_resumes = {}, {}
        for r in snapshots:
            if r.get("vacancyId") and not r.get("kind"):
                key = vacancy_key(r, "vacancyId")
                snapshot_owners.setdefault(key, set()).add(self.owner(r))
                if r.get("resumeId"):
                    snapshot_resumes.setdefault(key, set()).add(str(r["resumeId"]))
        vacancy_owners.update(snapshot_owners)
        for r in records:
            if self.owner(r) is not None or r.get("direction") or any(r.get(k) for k in ("resume_id", "resumeId", "requested_resume_id", "hh_resume_hash", "hh_resume_numeric_id")):
                continue
            candidates = set()
            if r.get("event") in {"negotiation_status", "negotiation_observation", "invitation", "screening_observation"}:
                key = vacancy_key(r)
                candidates = vacancy_owners.get(key, set())
                resumes = snapshot_resumes.get(key, set())
                if len(resumes) == 1:
                    r["hh_resume_numeric_id"] = next(iter(resumes))
                elif key not in snapshot_owners and len(application_resumes.get(key, set())) == 1:
                    r["hh_resume_hash"], r["hh_resume_numeric_id"] = next(iter(application_resumes[key]))
            else:
                candidates = run_owners.get(r.get("run_id"), set())
            if len(candidates) == 1 and None not in candidates:
                r["direction"] = next(iter(candidates))
                if r.get("hh_resume_numeric_id"):
                    hashes = {v["hh_resume_hash"] for d in self.directions for v in d["variants"]
                              if str(v.get("hh_resume_numeric_id")) == str(r["hh_resume_numeric_id"])}
                    if len(hashes) == 1:
                        r["hh_resume_hash"] = next(iter(hashes))
        for r in records:
            owner = self.owner(r)
            # Normalize uniquely recovered owners for downstream joins. Keep
            # contradictory original evidence intact for the unspecified view.
            r["_history_owner"] = owner
            if owner:
                r["direction"] = owner
        return records


def for_events(events_file=None, scope="active"):
    import config
    return HistoryScope(Path(events_file).parent if events_file else config.JOB_HUNTER_HOME, scope)
