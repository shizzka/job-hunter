"""Resolve an explicitly configured title, never a first/fuzzy account resume."""


def exact_title_resume_id(resumes: list[dict], title: str) -> str:
    wanted = " ".join(str(title or "").casefold().split())
    if not wanted or not isinstance(resumes, list):
        return ""
    matches = set()
    for resume in resumes:
        if not isinstance(resume, dict):
            return ""
        resume_title = str(resume.get("title") or "")
        titles = [resume_title, *resume_title.splitlines()]
        if any(" ".join(value.casefold().split()) == wanted for value in titles):
            resume_id = str(resume.get("id") or "").strip()
            if not resume_id:
                return ""
            matches.add(resume_id)
    return next(iter(matches)) if len(matches) == 1 else ""
