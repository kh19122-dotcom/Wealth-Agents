import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_QUERY_KEYS = {
    "ref",
    "source",
    "share",
    "smid",
    "cmpid",
    "cmp",
    "ncid",
    "mkt_tok",
    "igshid",
    "spm",
    "fbclid",
    "gclid",
    "dclid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "wt_mc",
    "ga_source",
    "ga_medium",
    "ga_campaign",
}
TRACKING_QUERY_PREFIXES = ("utm_", "ga_", "fb_", "mc_")


def now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: str) -> str:
    value = value or ""
    value = re.sub(r"\s+", " ", value.strip())
    return value


def sentence_from_title(title: str) -> str:
    text = normalize_text(title)
    if not text:
        return "This item is included in the weekly report."
    if not text.endswith((".", "!", "?")):
        text = text + "."
    return f"This item covers: {text}"


def sanitize_html_summary(value: str, title_fallback: str = "") -> str:
    raw = value or ""
    raw = html.unescape(raw)
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<img\b[^>]*>", " ", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    raw = normalize_text(raw)
    if raw:
        return raw
    if title_fallback:
        return sentence_from_title(title_fallback)
    return ""


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query_pairs = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k in TRACKING_QUERY_KEYS:
            continue
        if any(k.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((k, val))

    query_pairs.sort()
    query = urlencode(query_pairs)
    return urlunsplit((scheme, netloc, path, query, ""))


def parse_datetime_to_iso(value: object) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_item_id(url: str, title: str, published_at: Optional[str]) -> str:
    canonical_url = canonicalize_url(url)
    normalized_title = normalize_text(title).lower()
    normalized_published = (published_at or "").strip()
    payload = f"{canonical_url}|{normalized_title}|{normalized_published}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
