"""Local, profile-scoped BM25 retrieval. No model calls or optional dependencies.

Indexes are immutable operation snapshots. Mandatory boundaries and the bounded
core profile prefix are included whole, or KnowledgeBudgetError is raised.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import threading
import unicodedata


class KnowledgeBudgetError(ValueError):
    """The requested budget cannot safely hold all mandatory knowledge."""


class KnowledgeSourceError(ValueError):
    """A source is unsafe, unreadable, or changed while being indexed."""


def retrieval_enabled() -> bool:
    return os.environ.get("KB_RETRIEVAL_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


_WORD = re.compile(r"[a-zа-я0-9]+(?:[+#]+|[/.-][a-zа-я0-9]+)*", re.I)
_SUFFIX = re.compile(r"(иями|ями|ами|ого|ему|ому|ыми|ими|иях|ией|иям|ах|ях|ию|ия|ии|ий|ый|ой|ей|ая|яя|ое|ее|ые|ие|ов|ев|ам|ям|ом|ем|ы|и|а|я|у|ю|е|о)$")
_STOP = frozenset("и в во на с со к по из от до за для не нет что как это или ли а но был была были быть опыт работа работы вы мы я у о об the a an of in to and or with".split())
_GROUPS = (
    ("android", "андроид"), ("excel", "эксель", "эксэл", "эксель"),
    ("1c", "1с"), ("automation", "автоматизация", "автоматизированный", "автотесты", "aqa"),
    ("testing", "тестирование", "тестировать"), ("manual", "ручной"),
    ("postgresql", "postgres", "постгрес"), ("javascript", "js"),
    ("python", "питон"), ("api", "апи"), ("sql", "скл"),
)
_CORE_NAMES = frozenset({"profile_note.md", "core_profile.md", "canonical_profile.md", "candidate_profile.md"})
_BOUNDARY_FILES = frozenset({"constraints.md", "limits.md", "do_not_claim.md"})
_BOUNDARY_LABELS = frozenset({"LIMIT", "LIMITS", "DO_NOT_CLAIM", "FORBIDDEN_CLAIMS"})
_TAG = re.compile(r"(?<!\S)(?:[-*+]\s+)?\[([\w -]+)\]\s*")
_NUMBERED_SECTION = re.compile(r"(?<!\S)\d{1,3}[.)]\s+([^\n\[\]]{1,120}?)\s+(?=(?:[-*+]\s+)?\[[\w -]+\])")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_MAX_CHUNK = 1100
_CORE_LIMIT = 1500


def _boundary_label(text: str) -> bool:
    return re.sub(r"[ -]+", "_", text.strip().strip("[]").upper()) in _BOUNDARY_LABELS


def _base_tokens(text: str) -> list[str]:
    result = []
    for word in _WORD.findall(text.lower().replace("ё", "е")):
        if word in _STOP:
            continue
        if len(word) > 4 and re.fullmatch("[а-я]+", word):
            stem = _SUFFIX.sub("", word)
            if len(stem) >= 3:
                word = stem
        result.append(word)
    return result


def _aliases(extra: str) -> tuple[tuple[str, str], ...]:
    groups = list(_GROUPS)
    for line in extra.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        terms = [item.strip() for item in re.split(r"\s*(?:=>|->|=|:|,|;|\|)\s*", line) if item.strip()]
        if len(terms) > 1:
            groups.append(tuple(terms))
    # Merge overlapping groups, including local additions to built-in aliases.
    parents: dict[str, str] = {}

    def root(term: str) -> str:
        while parents[term] != term:
            term = parents[term]
        return term

    for group in groups:
        tokens = [_base_tokens(term) for term in group]
        terms = [term[0] for term in tokens if len(term) == 1]
        for term in terms:
            parents.setdefault(term, term)
        if terms:
            canonical = root(terms[0])
            for term in terms[1:]:
                parents[root(term)] = canonical
    return tuple(sorted((term, root(term)) for term in parents))


def _tokens(text: str, aliases: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    mapping = dict(aliases)
    return tuple(mapping.get(term, term) for term in _base_tokens(text))


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    heading: str
    text: str
    mandatory: bool
    tokens: tuple[str, ...]

    def render(self) -> str:
        label = self.source + (" > " + self.heading if self.heading else "")
        return f"### {label}\n{self.text}"


@dataclass(frozen=True)
class KnowledgeIndex:
    profile_root: str
    fingerprint: tuple[tuple[str, int, int], ...]
    chunks: tuple[KnowledgeChunk, ...]
    aliases: tuple[tuple[str, str], ...]
    document_frequencies: tuple[tuple[str, int], ...]
    average_length: float

    def to_snapshot(self) -> str:
        """Serialize the complete index, never paths to be re-read later."""
        return json.dumps({
            "version": 1, "profile_root": self.profile_root,
            "fingerprint": self.fingerprint,
            "chunks": [[c.source, c.heading, c.text, c.mandatory, c.tokens] for c in self.chunks],
            "aliases": self.aliases, "document_frequencies": self.document_frequencies,
            "average_length": self.average_length,
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_snapshot(cls, snapshot: str) -> KnowledgeIndex:
        """Restore a trusted internal operation snapshot without reading files."""
        data = json.loads(snapshot)
        if data.get("version") != 1:
            raise ValueError("Unsupported knowledge snapshot version")
        return cls(data["profile_root"], tuple(tuple(v) for v in data["fingerprint"]),
                   tuple(KnowledgeChunk(c[0], c[1], c[2], c[3], tuple(c[4])) for c in data["chunks"]),
                   tuple(tuple(v) for v in data["aliases"]),
                   tuple(tuple(v) for v in data["document_frequencies"]), data["average_length"])

    def retrieve(self, query: str, *, limit_chars: int = 6000) -> str:
        if not isinstance(limit_chars, int) or isinstance(limit_chars, bool) or limit_chars < 0:
            raise ValueError("limit_chars must be a non-negative integer")
        mandatory = [chunk.render() for chunk in self.chunks if chunk.mandatory]
        required = len("\n\n".join(mandatory))
        if required > limit_chars:
            raise KnowledgeBudgetError(
                f"Mandatory knowledge requires {required} characters; budget is {limit_chars}. "
                "Candidate boundaries cannot be omitted."
            )
        selected = list(mandatory)
        used = required
        query_terms = sorted(set(_tokens(str(query or ""), self.aliases)))
        df = dict(self.document_frequencies)
        count = len(self.chunks)
        ranked = []
        for order, chunk in enumerate(self.chunks):
            if chunk.mandatory:
                continue
            frequency = Counter(chunk.tokens)
            score = 0.0
            for term in query_terms:
                tf = frequency.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (count - df[term] + 0.5) / (df[term] + 0.5))
                normalizer = 1.2 * (0.25 + 0.75 * len(chunk.tokens) / (self.average_length or 1))
                score += idf * tf * 2.2 / (tf + normalizer)
            # Stable zero-score fallback gives generic questions useful profile
            # context after all positively matched chunks have been considered.
            ranked.append((-score, order, chunk))
        for _, _, chunk in sorted(ranked):
            rendered = chunk.render()
            cost = len(rendered) + (2 if selected else 0)
            if used + cost <= limit_chars:
                selected.append(rendered)
                used += cost
        return "\n\n".join(selected)


def _paragraph_chunks(text: str):
    """Bound long paragraphs at whitespace; retain every source character."""
    current = ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        while len(paragraph) > _MAX_CHUNK:
            if current:
                yield current
                current = ""
            split = paragraph.rfind(" ", 0, _MAX_CHUNK + 1)
            if split < _MAX_CHUNK // 2:
                split = _MAX_CHUNK
            yield paragraph[:split]
            paragraph = paragraph[split:].strip()
        if current and len(current) + 2 + len(paragraph) > _MAX_CHUNK:
            yield current
            current = ""
        current = current + "\n\n" + paragraph if current else paragraph
    if current:
        yield current


def _markdown_parts(text: str):
    headings: list[tuple[int, str]] = []
    lines: list[str] = []

    def section():
        content = "\n".join(lines).strip()
        heading = " > ".join(title for _, title in headings)
        blocks = []
        # Horizontal rules still end a tagged item in flattened Markdown.
        # Otherwise the last tag swallows the next untagged section wholesale.
        for segment in re.split(r"(?<!\S)---+(?=\s|$)", content):
            numbered = list(_NUMBERED_SECTION.finditer(segment))
            if numbered:
                blocks.append((heading, segment[:numbered[0].start()]))
                for pos, match in enumerate(numbered):
                    end = numbered[pos + 1].start() if pos + 1 < len(numbered) else len(segment)
                    context = " > ".join(filter(None, (heading, match.group(1).strip())))
                    blocks.append((context, segment[match.end():end]))
            else:
                blocks.append((heading, segment))
        for context, block in blocks:
            matches = list(_TAG.finditer(block))
            pieces = []
            if matches:
                if block[:matches[0].start()].strip():
                    pieces.append(("", block[:matches[0].start()].strip()))
                for pos, match in enumerate(matches):
                    end = matches[pos + 1].start() if pos + 1 < len(matches) else len(block)
                    pieces.append((match.group(1), block[match.end():end].strip()))
            elif block.strip():
                pieces.append(("", block.strip()))
            for tag, piece in pieces:
                mandatory = _boundary_label(tag) or any(_boundary_label(title) for title in context.split(" > "))
                # Boundaries stay intact, including a long multi-paragraph prohibition.
                parts = [piece] if mandatory else _paragraph_chunks(piece)
                for part in parts:
                    yield context, (f"- [{tag}] {part}" if tag else part), mandatory

    in_fence = False
    for line in text.splitlines():
        if re.match(r"\s*(```|~~~)", line):
            in_fence = not in_fence
        match = _HEADING.match(line) if not in_fence else None
        if match:
            yield from section()
            lines.clear()
            level = len(match.group(1))
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, match.group(2)))
        else:
            lines.append(line)
    yield from section()


def _knowledge_root(root: Path) -> Path:
    knowledge = root / "knowledge"
    if not knowledge.is_symlink():
        return knowledge
    # A named direction can explicitly share this owner's common knowledge.
    # Never follow arbitrary links or another direction's private directory.
    shared = root.parent.parent / "knowledge"
    if (root.parent.name != "profiles" or shared.is_symlink() or not shared.is_dir()
            or knowledge.resolve(strict=True) != shared):
        raise KnowledgeSourceError("Knowledge link must target the common home/knowledge directory")
    return shared


def _sources(root: Path) -> tuple[Path, ...]:
    knowledge = root / "knowledge"
    knowledge_root = _knowledge_root(root)
    paths = list(knowledge.glob("*.md")) if knowledge.is_dir() else []
    paths.extend(root / name for name in ("facts.json", "candidate_interview.json") if (root / name).exists() or (root / name).is_symlink())
    synonyms = knowledge / "synonyms.txt"
    if synonyms.exists() or synonyms.is_symlink():
        paths.append(synonyms)
    for path in paths:
        allowed_root = knowledge_root if path.parent == knowledge else root
        if path.is_symlink() or not path.resolve().is_relative_to(allowed_root):
            raise KnowledgeSourceError(f"Unsafe knowledge source: {path.name}")
        if not path.is_file():
            raise KnowledgeSourceError(f"Knowledge source is not a regular file: {path.name}")
    return tuple(sorted(paths, key=lambda path: str(path.relative_to(root))))


def _fingerprint(root: Path, paths: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
    result = []
    for path in paths:
        stat = path.stat()
        result.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    return tuple(result)


def _read_source(root: Path, path: Path, expected: tuple[str, int, int]) -> str:
    # Anchor each open to directory descriptors. O_NOFOLLOW on both the
    # knowledge directory and leaf prevents a concurrent symlink replacement
    # from ever reading another profile (a post-read check alone is too late).
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if path.parent != root:
            if (root / "knowledge").is_symlink():
                _knowledge_root(root)  # Revalidate before opening; don't follow the link itself.
                home = os.open("../..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    child = os.open("knowledge", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
                finally:
                    os.close(home)
            else:
                child = os.open("knowledge", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_mtime_ns, before.st_size) != expected[1:]:
                raise KnowledgeSourceError("Knowledge source changed before read")
            text = stream.read()
            after = os.fstat(stream.fileno())
            if (after.st_mtime_ns, after.st_size) != expected[1:]:
                raise KnowledgeSourceError("Knowledge source changed during read")
            return text
    finally:
        os.close(directory)


def _value_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_parts(name: str, text: str):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise KnowledgeSourceError(f"Expected an object in {name}")
    if name == "facts.json":
        confirmed = data.get("confirmed")
        if isinstance(confirmed, dict):
            for key, value in sorted(confirmed.items()):
                if value is not None and value != "" and value != [] and value != {}:
                    yield "CONFIRMED", f"{key}: {_value_text(value)}", False
        for key in ("weak", "allowed_wording", "do_not_claim", "forbidden_claims"):
            value = data.get(key)
            if value is not None and value != "" and value != [] and value != {}:
                # Lists contain individual guardrails, not one JSON blob. Keep
                # each intact so a duplicate in Markdown can be removed safely.
                for item in value if isinstance(value, list) else [value]:
                    if item is not None and item != "" and item != [] and item != {}:
                        yield "LIMIT / " + key.upper(), _value_text(item), True
    else:
        items = data.get("facts", [])
        if not isinstance(items, list):
            raise KnowledgeSourceError("Expected candidate_interview facts list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
                continue
            topic = str(item.get("topic", "общий"))
            mandatory = topic.strip().casefold() in {"границы", "ограничения"} or _boundary_label(topic)
            yield ("LIMIT" if mandatory else "CONFIRMED") + " > " + topic, item["text"].strip(), mandatory
            if not mandatory:
                # Interview entries retain their paragraph context for ranking.
                # Embedded tagged guardrails additionally get mandatory copies.
                for heading, body, tagged in _markdown_parts(item["text"]):
                    if tagged:
                        context = " > ".join(filter(None, ("LIMIT", topic, heading)))
                        yield context, body, True


def _deduplicate_boundaries(chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    """Prefer tagged Markdown over identical structured guardrails.

    Only presentation, Unicode form, case and whitespace are normalized. No
    stemming, substring matching or semantic equivalence can erase a boundary.
    """
    seen = set()
    keep = set()
    candidates = sorted(enumerate(chunks), key=lambda pair: (not pair[1].source.endswith(".md"), pair[0]))
    for pos, chunk in candidates:
        if chunk.mandatory and chunk.source in {"facts.json", "candidate_interview.json", "knowledge/candidate_facts.md"}:
            tag = _TAG.match(chunk.text)
            body = chunk.text[tag.end():] if tag else chunk.text
            key = " ".join(unicodedata.normalize("NFKC", body).casefold().split())
            if key in seen:
                continue
            seen.add(key)
        keep.add(pos)
    return [chunk for pos, chunk in enumerate(chunks) if pos in keep]


def _group_boundaries(chunks: list[KnowledgeChunk], aliases) -> list[KnowledgeChunk]:
    """Pay the source/heading label once for mandatory items in a section."""
    result = []
    positions = {}
    for chunk in chunks:
        key = (chunk.source, chunk.heading)
        if chunk.mandatory and key in positions:
            pos = positions[key]
            previous = result[pos]
            body = previous.text + "\n\n" + chunk.text
            result[pos] = KnowledgeChunk(chunk.source, chunk.heading, body, True,
                                         _tokens(chunk.heading + " " + chunk.heading + " " + body, aliases))
        else:
            if chunk.mandatory:
                positions[key] = len(result)
            result.append(chunk)
    return result


_CACHE: OrderedDict[str, KnowledgeIndex] = OrderedDict()
_CACHE_LOCK = threading.RLock()


def get_knowledge_index(profile_dir: str | os.PathLike) -> KnowledgeIndex:
    """Capture a source snapshot; a later operation sees newly changed sources.

    Sources are local to the resolved profile root, or its explicitly linked
    common home/knowledge directory. Symlinked source files remain rejected.
    """
    root = Path(profile_dir).expanduser().resolve()
    try:
        paths = _sources(root)
        fingerprint = _fingerprint(root, paths)
        with _CACHE_LOCK:
            cached = _CACHE.get(str(root))
            if cached is not None and cached.fingerprint == fingerprint:
                _CACHE.move_to_end(str(root))
                return cached
        contents = {str(path.relative_to(root)): _read_source(root, path, expected)
                    for path, expected in zip(paths, fingerprint)}
        if _sources(root) != paths or _fingerprint(root, paths) != fingerprint:
            raise KnowledgeSourceError("Knowledge sources changed during snapshot; retry the operation")
        aliases = _aliases(contents.get("knowledge/synonyms.txt", ""))
        chunks = []
        for name, text in contents.items():
            if name.endswith("synonyms.txt"):
                continue
            filename = Path(name).name.lower()
            parts = _markdown_parts(text) if name.endswith(".md") else _json_parts(filename, text)
            core_remaining = _CORE_LIMIT if filename in _CORE_NAMES else 0
            for heading, body, mandatory in parts:
                mandatory = mandatory or filename in _BOUNDARY_FILES
                bodies = []
                if not mandatory and core_remaining:
                    split = min(len(body), core_remaining)
                    if split < len(body):
                        space = body.rfind(" ", 0, split + 1)
                        if space > 0:
                            split = space
                    bodies.append((body[:split].strip(), True))
                    # Once the prefix ends, later sections cannot use a few
                    # spare characters left by the whitespace-aligned cut.
                    core_remaining = 0 if split < len(body) else core_remaining - split
                    body = body[split:].strip()
                elif mandatory:
                    core_remaining = max(0, core_remaining - len(body))
                if body:
                    bodies.extend((part, mandatory) for part in ([body] if mandatory else _paragraph_chunks(body)))
                for part, required in bodies:
                    tokens = _tokens(heading + " " + heading + " " + part, aliases)
                    chunks.append(KnowledgeChunk(name, heading, part, required, tokens))
        chunks = _group_boundaries(_deduplicate_boundaries(chunks), aliases)
        frequencies: Counter = Counter()
        for chunk in chunks:
            frequencies.update(set(chunk.tokens))
        index = KnowledgeIndex(str(root), fingerprint, tuple(chunks), aliases, tuple(sorted(frequencies.items())),
                               sum(len(chunk.tokens) for chunk in chunks) / len(chunks) if chunks else 0)
        with _CACHE_LOCK:
            _CACHE[str(root)] = index
            _CACHE.move_to_end(str(root))
            while len(_CACHE) > 16:
                _CACHE.popitem(last=False)
        return index
    except KnowledgeSourceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise KnowledgeSourceError(f"Cannot load profile knowledge: {type(exc).__name__}") from exc


def retrieve_knowledge(query: str, *, profile_dir: str | os.PathLike, limit_chars: int = 6000) -> str:
    return get_knowledge_index(profile_dir).retrieve(query, limit_chars=limit_chars)
