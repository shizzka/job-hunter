"""Live, read-only check of an hh session and its resume catalog (one GET, no browser).

Used by «🚀 С чего начать» and «Проверить резюме»: saved cookies alone do not
prove the session still works (hh may answer with a captcha or a login page).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

OK, CHALLENGE, LOGIN, ERROR = "ok", "challenge", "login", "error"
MESSAGES = {
    OK: "",
    CHALLENGE: "hh просит проверку «я не робот» — войдите в hh.ru заново через бота.",
    LOGIN: "сессия hh устарела — войдите в hh.ru заново через бота.",
    ERROR: "hh сейчас не ответил — попробуйте ещё раз через минуту.",
}


def probe(cookies_file: str, *, client_factory=None) -> dict:
    """Return {"status", "message", "resumes": [{"id", "title"}]}."""
    from hh.resume_refresh import ResumeHTML, _catalog_title
    if client_factory is None:
        from hh_response_counter import _authenticated_client as client_factory
    try:
        with client_factory(cookies_file) as client:
            response = client.get("https://hh.ru/applicant/resumes")
    except Exception:
        return {"status": ERROR, "message": MESSAGES[ERROR], "resumes": []}
    path = urlsplit(str(response.url)).path
    if path.startswith(("/account/login", "/auth/")):
        return {"status": LOGIN, "message": MESSAGES[LOGIN], "resumes": []}
    parser = ResumeHTML()
    parser.feed(response.text)
    parser.close()
    if response.status_code == 403 or (parser.challenge and not parser.catalog_marker):
        return {"status": CHALLENGE, "message": MESSAGES[CHALLENGE], "resumes": []}
    if response.status_code != 200 or not parser.catalog_marker:
        return {"status": LOGIN, "message": MESSAGES[LOGIN], "resumes": []}
    resumes, seen = [], set()
    for node in parser.nodes:
        if node["tag"] != "a":
            continue
        match = re.fullmatch(r"/resume/([A-Za-z0-9_-]+)/?", urlsplit(node["attrs"].get("href", "")).path)
        if not match or match[1] in seen:
            continue
        seen.add(match[1])
        resumes.append({"id": match[1], "title": _catalog_title(" ".join(" ".join(node["text"]).split()))})
    return {"status": OK, "message": "", "resumes": resumes}


def save_catalog(catalog_path: str, resumes: list[dict]) -> None:
    """Refresh the cached catalog shown by «Мои резюме» (keeps previous entries' extra fields)."""
    from state_store.hh_resume_import import ResumeCatalogRepository
    repository = ResumeCatalogRepository(catalog_path)
    previous, revision = repository.snapshot()
    extra = {item["id"]: item for item in previous or [] if isinstance(item, dict) and item.get("id")}
    merged = [{**extra.get(item["id"], {}), **item} for item in resumes]
    repository.save(merged, expected_revision=revision)
