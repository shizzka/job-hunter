"""Клиент для поиска вакансий на GeekJob."""
import html
import logging
import re

import aiohttp

import config

log = logging.getLogger("geekjob_client")


def _clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<hr\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</section>|</article>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"</li>|</ul>|</ol>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S | re.I)
    return match.group(1) if match else ""


class GeekJobClient:
    """Парсит публичный SSR-листинг вакансий GeekJob без браузера."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept-Language": "ru,en;q=0.9",
                },
                timeout=aiohttp.ClientTimeout(total=20),
                trust_env=True,
            )

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_text(self, url: str) -> str:
        await self.start()
        assert self._session is not None
        async with self._session.get(url, ssl=False) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GeekJob error {resp.status}: {text[:300]}")
            return await resp.text()

    def _build_list_url(self, page: int) -> str:
        if page <= 1:
            return f"{config.GEEKJOB_BASE_URL}/vacancies"
        return f"{config.GEEKJOB_BASE_URL}/vacancies/{page}"

    def _extract_total_pages(self, page_text: str) -> int:
        raw = _first_match(page_text, r"<small>\s*страниц\s+(\d+)\s*</small>")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _extract_list_items(self, page_text: str) -> list[str]:
        serplist = _first_match(
            page_text,
            r'<ul class="collection serp-list" id="serplist">(.*?)</ul>',
        )
        if not serplist:
            return []
        return re.findall(
            r'<li class="collection-item avatar[^"]*">(.*?)</li>',
            serplist,
            re.S | re.I,
        )

    def _normalize_vacancy(self, item_html: str) -> dict | None:
        href = _first_match(item_html, r'href="(/vacancy/[^"]+)"')
        title = _clean_html(_first_match(item_html, r'<a href="[^"]+" class="title"[^>]*>(.*?)</a>'))
        company = _clean_html(
            _first_match(item_html, r'<p class="truncate company-name">\s*<a [^>]*>(.*?)</a>\s*</p>')
        )
        top_info = _first_match(
            item_html,
            r'<div class="info">\s*<a href="/vacancy/[^"]+"[^>]*>(.*?)</a>\s*</div>',
        )
        labels_html = _first_match(
            item_html,
            r'<p class="truncate company-name">.*?</p>\s*<div class="info">(.*?)</div>',
        )
        published_at = _clean_html(
            _first_match(item_html, r'<time class="truncate datetime-info">\s*<a [^>]*>(.*?)</a>')
        )

        if not href or not title:
            return None

        location_html, _, salary_html = top_info.partition("<br")
        salary = _clean_html(_first_match(top_info, r'<span class="salary">(.*?)</span>')) or "не указана"
        location = _clean_html(location_html)
        labels = [
            _clean_html(text)
            for text in re.findall(r"<span class=\"[^\"]+\">(.*?)</span>", labels_html, re.S | re.I)
            if _clean_html(text)
        ]

        snippet_parts = []
        if location:
            snippet_parts.append(f"Локация: {location}")
        if labels:
            snippet_parts.append("Формат: " + ", ".join(labels))
        if published_at:
            snippet_parts.append(f"Опубликовано: {published_at}")
        snippet = "\n".join(snippet_parts)

        external_id = href.rstrip("/").split("/")[-1]
        return {
            "id": f"geekjob:{external_id}",
            "external_id": external_id,
            "source": "geekjob",
            "source_label": "GeekJob",
            "title": title or "Без названия",
            "company": company or "—",
            "salary": salary,
            "url": f"{config.GEEKJOB_BASE_URL}{href}",
            "snippet": snippet[:1000],
            "details": snippet,
            "location": location,
            "apply_mode": "manual",
        }

    async def search_vacancies(self, page: int = 1) -> tuple[list[dict], int]:
        page_text = await self._get_text(self._build_list_url(page))
        total_pages = self._extract_total_pages(page_text)

        normalized = []
        for item_html in self._extract_list_items(page_text):
            vacancy = self._normalize_vacancy(item_html)
            if vacancy is not None:
                normalized.append(vacancy)
        return normalized, total_pages

    async def get_vacancy_details(self, url: str) -> str:
        page_text = await self._get_text(url)

        company = _clean_html(_first_match(page_text, r'<h5 class="company-name">(.*?)</h5>'))
        location = _clean_html(_first_match(page_text, r'<div class="location">(.*?)</div>'))
        category = _clean_html(_first_match(page_text, r'<div class="category">(.*?)</div>'))
        jobinfo = _clean_html(_first_match(page_text, r'<div class="jobinfo">(.*?)</div>'))
        published_at = _clean_html(_first_match(page_text, r'<div class="time">(.*?)</div>'))
        description = _clean_html(
            _first_match(page_text, r'<div id="vacancy-description">(.*?)</div>')
        )

        tag_blocks = re.findall(r'<div class="tags">(.*?)</div>', page_text, re.S | re.I)
        tags = []
        for block in tag_blocks:
            cleaned = _clean_html(block)
            if cleaned:
                tags.append(cleaned)

        parts = []
        if company:
            parts.append(f"Компания: {company}")
        if location:
            parts.append(f"Локация: {location}")
        if category:
            parts.append(f"Уровень: {category}")
        if jobinfo:
            parts.append(f"Условия: {jobinfo}")
        if tags:
            parts.append("Теги: " + " | ".join(tags))
        if published_at:
            parts.append(f"Опубликовано: {published_at}")
        if description:
            parts.append(f"Описание:\n{description}")

        if parts:
            return "\n\n".join(parts)

        fallback = _clean_html(
            _first_match(page_text, r'<meta name="description" content="([^"]+)"')
        )
        if fallback:
            log.warning("GeekJob details fallback used for %s", url)
        return fallback
