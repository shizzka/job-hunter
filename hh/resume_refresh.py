"""Read resume server HTML through the authenticated context, without site JS."""
import re
import hashlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import config
from hh.ui import CHALLENGE_TEXT


SECTIONS = {
    "resume-block-salary": "Зарплата",
    "resume-position-card": "Позиция",
    "resume-list-card-experience": "Опыт работы",
    "skills-card": "Навыки",
    # "skills-methods" is hh's own promo of skill tests (often from another profession),
    # not resume content: it is deliberately not imported.
    "resume-list-card-education": "Образование",
    "resume-about-card": "О себе",
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ResumeHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes = [], []
        self.catalog_marker = False
        self.challenge = False
        self.source = ""

    def feed(self, data):
        self.source += data
        super().feed(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        inert = tag in {"script", "style", "template", "noscript"} or any(n["inert"] for n in self.stack)
        qa = attrs.get("data-qa", "")
        if not inert:
            if qa == "resume" or qa.startswith("resume-card-link-"):
                self.catalog_marker = True
            identity = " ".join(attrs.get(k, "") for k in ("id", "class", "data-qa", "src"))
            self.challenge |= "captcha" in identity.casefold()
        capture = not inert and (tag == "a" or qa in SECTIONS or qa == "resume-block-title-position")
        node = {"tag": tag, "attrs": attrs, "inert": inert, "text": [] if capture else None}
        if capture:
            self.nodes.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if any(n["inert"] for n in self.stack):
            return
        self.challenge |= any(cue in data.casefold() for cue in CHALLENGE_TEXT)
        for node in self.stack:
            if node["text"] is not None:
                node["text"].append(data)


def _trusted_url(url, path):
    parsed = urlsplit(url)
    paths = (path,) if isinstance(path, str) else path
    if (parsed.scheme != "https" or not parsed.hostname
            or not (parsed.hostname == "hh.ru" or parsed.hostname.endswith(".hh.ru"))
            or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.path.rstrip("/") not in paths):
        raise ValueError("HH перенаправил запрос: нужен вход HH или ручная проверка страницы резюме.")


async def _server_html(session, path):
    context = session._context
    if context is None:
        raise RuntimeError("Сессия скачивания HH недоступна.")
    url = config.HH_BASE_URL.rstrip("/") + path
    paths = (path, "/applicant/profile/me") if path == "/applicant/resumes" else (path,)
    for _ in range(4):
        _trusted_url(url, paths)
        session._raise_stopped_ui("resume_refresh_before_get")
        response = await context.request.get(url, max_redirects=0, timeout=30000)
        try:
            if session._context is not context:
                raise RuntimeError("Сессия скачивания HH изменилась.")
            session._raise_stopped_ui("resume_refresh_after_get")
            _trusted_url(response.url, paths)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("HH вернул перенаправление без адреса; нужна ручная проверка.")
                url = urljoin(url, location)
                _trusted_url(url, paths)
                continue
            if response.status != 200:
                raise ValueError("HH не вернул резюме. Проверьте вход HH и доступ к выбранному ID.")
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html":
                raise ValueError("HH вернул неизвестный формат вместо страницы резюме.")
            body = await response.text()
            if session._context is not context:
                raise RuntimeError("Сессия скачивания HH изменилась.")
            session._raise_stopped_ui("resume_refresh_after_read")
            if re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', body, re.I):
                raise ValueError("Сессия HH недоступна. Выполните «Вход HH» и повторите обновление.")
            parser = ResumeHTML()
            parser.feed(body)
            parser.close()
            if parser.challenge:
                raise ValueError("HH требует captcha или проверку браузера. Выполните «Вход HH» вручную.")
            return parser, response.url
        finally:
            await response.dispose()
    raise ValueError("Слишком много перенаправлений HH; прежнее резюме сохранено.")


_EMPLOYMENT_PREFIX = re.compile(
    r"^(?:(?:постоянная работа|подработка|частичная занятость|проектная работа|стажировка|"
    r"волонт[её]рство|разовое задание|вахта)(?:,\s*|\s+и\s+|\s+))+(?:(?:и\s+)?ещ[её]\s+\d+\s+)?",
    re.IGNORECASE)
_SALARY_SUFFIX = re.compile(r"\s+(?:от\s+|до\s+)?\d[\d\s\u00a0\u202f]*\s*(?:₽|руб\.?|\$|€).*$", re.IGNORECASE)


def _catalog_title(text):
    """Catalog cards also render employment type and salary inside the resume link."""
    cleaned = _SALARY_SUFFIX.sub("", _EMPLOYMENT_PREFIX.sub("", text)).strip()
    return cleaned or text


async def get_resume_ids_readonly(session):
    parser, url = await _server_html(session, "/applicant/resumes")
    if not parser.catalog_marker:
        raise ValueError("Не удалось подтвердить вход HH и каталог резюме. Выполните «Вход HH».")
    records = {}
    for node in parser.nodes:
        if node["tag"] != "a":
            continue
        href = urljoin(url, node["attrs"].get("href", ""))
        match = re.fullmatch(r"/resume/([A-Za-z0-9_-]+)/?", urlsplit(href).path)
        if not match:
            continue
        try:
            _trusted_url(href, "/resume/" + match[1])
        except ValueError:
            continue
        title = _catalog_title(" ".join(" ".join(node["text"]).split()))
        records.setdefault(match[1], {"id": match[1], "title": title, "url": href,
                                       "hh_resume_numeric_id": extract_resume_numeric_id(parser.source, match[1])})
    return list(records.values())


async def download_resume_readonly(session, resume):
    resume_id = str(resume.get("id") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", resume_id) is None:
        raise ValueError("Некорректный ID резюме HH.")
    parser, _ = await _server_html(session, "/resume/" + resume_id)
    title, sections = "", {}
    for node in parser.nodes:
        qa = node["attrs"].get("data-qa", "")
        text = " ".join(" ".join(node["text"]).split())
        if qa == "resume-block-title-position":
            title = text
        elif qa in SECTIONS and text:
            sections[SECTIONS[qa]] = text
    if not title or not sections:
        raise ValueError("HH вернул пустое резюме или неизвестную страницу; локальный файл сохранён.")
    raw = f"# {title}\n\n" + "\n\n".join(f"## {name}\n{text}" for name, text in sections.items())
    return {"title": title, "sections": sections, "raw": raw,
            "hh_resume_hash": resume_id,
            "hh_resume_numeric_id": extract_resume_numeric_id(parser.source, resume_id),
            "text_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def extract_resume_numeric_id(html, resume_hash):
    """Use explicit hash↔numeric mappings in known resume state containers only.

    Vacancy IDs, user IDs and arbitrary nested ``id`` fields are never candidates.
    Missing/contradictory mappings stay unknown; titles never establish identity.
    """
    from hh.observation import initial_state, _number
    try:
        state = initial_state(html)
    except ValueError:
        return None
    if not isinstance(state, dict):
        return None
    candidates = set()
    numeric_hashes = {}
    containers = []
    for key in ("resume", "resumeView", "resumeData", "applicantResume", "resumes", "applicantResumes"):
        container = state.get(key)
        if not isinstance(container, (dict, list)):
            continue
        containers.append(container)
        if isinstance(container, dict):
            for child in ("resume", "resumes", "resumeList"):
                if isinstance(container.get(child), (dict, list)):
                    containers.append(container[child])
    for container in containers:
        items = [(None, container)] if isinstance(container, dict) else []
        if isinstance(container, dict):
            items += list(container.items())
        else:
            items += list(enumerate(container))
        for key, value in items:
            if not isinstance(value, dict):
                continue
            hashes = {value.get(field) for field in ("hash", "resumeHash", "@hash")
                      if isinstance(value.get(field), str) and value[field]}
            # Some resume-page states use id for the URL hash and resumeId for
            # the numeric database identity. Only that explicit pair is accepted.
            identity_hash = value.get("id")
            if (isinstance(identity_hash, str) and _number(identity_hash) is None
                    and _number(value.get("resumeId")) is not None):
                hashes.add(identity_hash)
            if resume_hash in hashes and hashes != {resume_hash}:
                return None
            numbers = {_number(value.get(field)) for field in ("id", "resumeId", "@id")}
            if isinstance(container, dict) and key is not None:
                numbers.add(_number(key))
            numbers = {number for number in numbers if number is not None and number > 0}
            for number in numbers:
                numeric_hashes.setdefault(number, set()).update(hashes)
            if hashes == {resume_hash}:
                candidates.update(numbers)
    if len(candidates) == 1:
        candidate = next(iter(candidates))
        if numeric_hashes[candidate] == {resume_hash}:
            return candidate
    return None
