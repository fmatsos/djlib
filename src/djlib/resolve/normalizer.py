import re
import unicodedata

_DASH_CHARS = '‐‑‒–—―−'
_QUOTE_MAP = {
    '‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"', '″': '"',
}
_WHITESPACE_RE = re.compile(r'\s+')


def normalize_identity(value: str) -> str:
    text = unicodedata.normalize('NFKC', value)
    for dash in _DASH_CHARS:
        text = text.replace(dash, '-')
    for quote, replacement in _QUOTE_MAP.items():
        text = text.replace(quote, replacement)
    text = text.replace('"', '')
    text = text.casefold()
    return _WHITESPACE_RE.sub(' ', text).strip()
