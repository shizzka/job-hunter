"""Read-only parsing of HH search HTML; never load resources or execute scripts."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from hh.response_path import RESPONSE_PATH, canonical_path, is_response_path


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)
    parent: object = None

    def walk(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.walk()

    def raw_text(self):
        text = "".join(c.raw_text() if isinstance(c, _Node) else c
                       for c in self.children)
        if self.tag in {"article", "section", "div", "p", "h1", "h2", "h3", "li", "br"}:
            return "\n" + text + "\n"
        return text

    def text(self):
        return " ".join(self.raw_text().split())


class _Tree(HTMLParser):
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _inert = {"script", "style", "template", "noscript", "iframe", "object", "embed"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.skipped = []
        self.resource_cues = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skipped:
            if tag not in self._void:
                self.skipped.append(tag)
            return
        if tag in self._inert:
            if tag == "iframe":
                self.resource_cues.append(attrs.get("src", ""))
            if tag not in self._void:
                self.skipped.append(tag)
            return
        node = _Node(tag, attrs, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        stack = self.skipped if self.skipped else [n.tag for n in self.stack]
        if tag in stack:
            index = len(stack) - 1 - stack[::-1].index(tag)
            if self.skipped:
                del self.skipped[index:]
            elif index:
                del self.stack[index:]

    def handle_data(self, data):
        if not self.skipped:
            self.stack[-1].children.append(data)


def _tree(html):
    tree = _Tree()
    tree.feed(html)
    tree.close()
    return tree


def _trusted_url(href, base_url):
    if not href or any(ord(c) < 33 for c in href) or "\\" in href:
        return ""
    url = urljoin(base_url, href)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or not (host == "hh.ru" or host.endswith(".hh.ru"))):
            return ""
    except ValueError:
        return ""
    return url


def _qa(node, values):
    return node.attrs.get("data-qa", "") in values


def _first(card, predicate):
    return next((n for n in card.walk() if predicate(n)), None)


def _ancestor_class_within(node, card, class_name):
    ancestor = node.parent
    while ancestor is not None and ancestor is not card:
        if class_name in ancestor.attrs.get("class", "").split():
            return True
        ancestor = ancestor.parent
    return False


def search_antibot_kind(html: str) -> str:
    """Identify challenge cues outside inert text without executing the challenge."""
    tree = _tree(html)
    text = tree.root.text()[:3000].lower()
    cues = " ".join(tree.resource_cues + [
        n.attrs.get(key, "") for n in tree.root.walk()
        for key in ("id", "class", "data-qa")]).lower()
    if any(term in text for term in ("ddos-guard", "ddos guard", "проверка браузера перед переходом на hh.ru", "не удалось проверить ваш браузер автоматически", "checking your browser before accessing")) or "ddos-guard" in cues:
        return "ddos_guard"
    if "captcha" in cues or any(term in text for term in ("введите символы", "подтвердите, что вы не робот", "текст с картинки", "i'm not a robot", "verify you are human")):
        return "captcha"
    if any(term in text for term in ("checking your browser", "проверка браузера", "проверяем ваш браузер", "verify your browser")):
        return "browser_check"
    return ""


def _empty_search_results(tree):
    for node in tree.root.walk():
        if _qa(node, {"empty-vacancy-search-block"}):
            return True
        if not _qa(node, {"vacancy-serp__results"}):
            continue
        # Beyond the last page HH renders an explicitly empty list, without
        # the zero-results placeholder. Ignore the CSS build hash.
        for child in node.walk():
            if (child.tag == "ol"
                    and any(c.startswith("vacancies-list--") for c in child.attrs.get("class", "").split())
                    and not any(child.walk()) and not child.text()):
                return True
    return False


def parse_hh_search_html(html: str, *, base_url: str) -> list[dict]:
    """Parse known SSR cards. Unknown search/challenge/modal states fail closed."""
    if not _trusted_url(base_url, base_url) or urlsplit(base_url).path != "/search/vacancy":
        raise ValueError("untrusted HH search URL")
    tree = _tree(html)
    if search_antibot_kind(html):
        raise ValueError("HH search challenge")
    for node in tree.root.walk():
        a = node.attrs
        if (a.get("role") == "dialog" or a.get("aria-modal") == "true"
                or (node.tag == "dialog" and "open" in a)
                or "modal" in a.get("data-qa", "").lower()):
            raise ValueError("unexpected HH search dialog")
    cards = [n for n in tree.root.walk() if _qa(n, {"serp-item", "vacancy-serp__vacancy"})]
    vacancies = []
    seen = set()
    if not cards:
        # Existing read-only metadata fallback, restricted to trusted numeric URLs.
        for link in tree.root.walk():
            if link.tag != "a":
                continue
            url = _trusted_url(link.attrs.get("href", ""), base_url)
            match = re.fullmatch(r"/vacancy/([0-9]+)/?", urlsplit(url).path) if url else None
            title = link.text()
            if not match or match[1] in seen or not 3 <= len(title) <= 200:
                continue
            nearby = link.parent
            while nearby is not None and nearby.tag != "root":
                classes = nearby.attrs.get("class", "")
                if nearby.tag in {"article", "section"} or "vacancy" in classes or "serp" in classes:
                    break
                nearby = nearby.parent
            if nearby is None or nearby.tag == "root":
                nearby = link.parent.parent if link.parent and link.parent.parent else link.parent
            company, salary = "", "не указана"
            for raw_line in nearby.raw_text().splitlines() if nearby else []:
                line = " ".join(raw_line.split())
                if "₽" in line or "$" in line or "руб" in line.lower():
                    salary = line
                elif line != title and not company and len(line) > 2:
                    company = line
            seen.add(match[1])
            vacancies.append({"id": match[1], "title": title, "company": company,
                              "salary": salary, "url": url, "snippet": "", "response_url": ""})
    for card in cards:
        title = _first(card, lambda n: n.tag == "a" and (
            _qa(n, {"serp-item__title", "serp__vacancy-title"})
            or "serp-item__title" in n.attrs.get("class", "").split()
            or (n.parent is not None and n.parent.tag in {"h2", "h3"})))
        if title is None:
            continue
        url = _trusted_url(title.attrs.get("href", ""), base_url)
        match = re.fullmatch(r"/vacancy/([0-9]+)/?", urlsplit(url).path) if url else None
        if not match or not title.text() or match[1] in seen:
            continue
        company = _first(card, lambda n: _qa(n, {"vacancy-serp__vacancy-employer", "serp-item__company"}) or (n.tag == "a" and _ancestor_class_within(n, card, "vacancy-serp-item__meta-info-company")))
        salary = _first(card, lambda n: _qa(n, {"vacancy-serp__vacancy-compensation", "serp-item__compensation"}) or "vacancy-serp-item__sidebar" in n.attrs.get("class", "").split())
        requirement = _first(card, lambda n: _qa(n, {"vacancy-serp__vacancy_snippet_requirement"}) or "g-user-content" in n.attrs.get("class", "").split())
        responsibility = _first(card, lambda n: _qa(n, {"vacancy-serp__vacancy_snippet_responsibility"}))
        response = _first(card, lambda n: n.tag == "a" and (_qa(n, {"vacancy-serp__vacancy_response"}) or is_response_path(urlsplit(n.attrs.get("href", "")).path)))
        response_url = _trusted_url(response.attrs.get("href", ""), base_url) if response else ""
        if response_url:
            parts = urlsplit(response_url)
            query = parse_qs(parts.query, keep_blank_values=True)
            if (canonical_path(parts.path) != RESPONSE_PATH
                    or query.get("vacancyId") != [match[1]]
                    or set(query) - {"vacancyId", "employerId", "hhtmFrom", "hhtmFromLabel"}):
                response_url = ""
        seen.add(match[1])
        vacancies.append({"id": match[1], "title": title.text(),
                          "company": company.text() if company else "—",
                          "salary": salary.text() if salary else "не указана",
                          "url": url, "snippet": "\n".join(n.text() for n in (requirement, responsibility) if n is not None and n.text()),
                          "response_url": response_url})
    if not vacancies and (cards or not _empty_search_results(tree)):
        raise ValueError("unrecognized HH search results")
    return vacancies
