"""Remove pasted list formatting without damaging technical role names."""
import re
import unicodedata


def clean_query(value: str) -> str:
    text = re.sub(r'^\s*№\s*\d{1,3}\s*[:.)-]?\s*', '', value or '')
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(' ' if c.isspace() else c for c in text if unicodedata.category(c) not in {'Cf', 'Cc'} or c.isspace())
    if re.fullmatch(r'\s*```[\w-]*\s*', text):
        return ''
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1', text)
    # Strip only anchored enumeration, never numbers within a role (1C, 3D, L2).
    text = re.sub(r'^\s*(?:[•●▪◦‣►✓✔✅🔹🔸📌🔍🔎]+\s*|[-–—*+]\s+|\[\s*[xX]?\s*\]\s*)+', '', text)
    text = re.sub(r'^\s*(?:\(?\d{1,3}[.)](?!\d)\s*|№\s*\d{1,3}\s*[:.)-]?\s*)', '', text)
    text = re.sub(r'^\s*#{1,6}\s+', '', text)
    # Decorative pictograms and punctuation are not part of vacancy names.
    text = ''.join(c if (c.isalnum() or c.isspace() or c in '.+#-/&()\"\'«»“”„’_*:') else ' ' for c in text)
    for _ in range(3):
        text = text.strip(' \t`*_«»“”„\"\'’:,')
        text = re.sub(r'^\s*(?:\(?\d{1,3}[.)](?!\d)\s*|[-–—+]\s+)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if any(c.isalnum() for c in text) else ''


def clean_query_list(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        query = clean_query(str(value or ''))
        if query and query.casefold() not in seen:
            seen.add(query.casefold())
            result.append(query)
    return result
