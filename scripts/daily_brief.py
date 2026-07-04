#!/usr/bin/env python3
"""Generate a concise daily news brief from RSS feeds and GDELT.

The script is intentionally dependency-free so it can run from Windows Task
Scheduler without a virtualenv. It collects recent items, filters noise,
deduplicates by title/link, scores for relevance, and writes Markdown + HTML.
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = Path(os.environ.get("NEWS_BRIEF_OUTPUT_DIR", ROOT / "public"))


BUCKET_KEYWORDS = {
    "global": [
        "world",
        "global",
        "economy",
        "climate",
        "war",
        "trade",
        "market",
        "security",
        "court",
        "policy",
        "regulation",
        "central bank",
    ],
    "india": [
        "india",
        "indian",
        "delhi",
        "mumbai",
        "supreme court",
        "rbi",
        "sebi",
        "parliament",
        "ministry",
        "budget",
        "policy",
        "regulation",
    ],
    "tech": [
        "ai",
        "artificial intelligence",
        "openai",
        "google",
        "microsoft",
        "apple",
        "nvidia",
        "semiconductor",
        "chip",
        "cyber",
        "security",
        "model",
        "startup",
        "funding",
    ],
    "local": [
        "mangalore",
        "mangaluru",
        "dakshina kannada",
        "coastal karnataka",
        "udupi",
        "kasaragod",
        "karwar",
        "bantwal",
        "puttur",
        "brahmavar",
        "vittal",
        "belthangady",
        "nethravati",
        "port",
        "rain",
    ],
}

EDITORIAL_CLASS_RULES = {
    "commercial": [
        "discount",
        "showroom",
        "grand opening",
        "furniture",
        "jewellery",
        "jewelry",
        "silver show",
        "gold rate",
        "sale",
        "offer",
        "inaugurates showroom",
        "expands footprint",
    ],
    "culture_lite": [
        "mahjong",
        "novel",
        "film",
        "celebrity",
        "recipe",
        "students excel",
        "school built",
        "csr funds",
        "seminar",
        "felicitation",
        "honoured",
        "honored",
        "esports",
        "chess.com",
        "world cup",
        "what the law says",
        "here's what",
        "viral reddit",
        "definition of success",
        "difficult childhood",
        "money can't buy happiness",
    ],
    "civic_safety": [
        "court",
        "arrested",
        "probe",
        "busted",
        "seized",
        "ordered",
        "deportation",
        "blocked",
        "fined",
        "rain",
        "flood",
        "dam",
        "accident",
        "fire",
        "cyber fraud",
        "pipeline",
        "drainage",
        "mining",
        "port",
        "policy",
    ],
    "high_impact": [
        "supreme court",
        "rbi",
        "sebi",
        "regulator",
        "billion",
        "million",
        "nationwide",
        "global",
        "war",
        "attack",
        "antitrust",
        "data leak",
        "cybersecurity",
        "artificial intelligence",
    ],
}

SECTION_MIN_SCORE = {
    "global": 14,
    "india": 14,
    "tech": 14,
    "local": 12,
}

TOP_EXCLUDED_CLASSES = {"commercial", "culture_lite"}
WATCHLIST_EXCLUDED_CLASSES = {"commercial", "culture_lite"}

BREAKING_TERMS = [
    "red alert",
    "earthquake",
    "cyclone",
    "flood",
    "landslide",
    "missile",
    "drone",
    "drones",
    "attack",
    "war",
    "sanctions",
    "market crash",
    "crash",
    "data leak",
    "breach",
    "cyberattack",
    "emergency",
    "evacuation",
    "ordered",
    "blocked",
    "banned",
    "fined",
    "suspended",
    "court rules",
    "court ordered",
    "supreme court",
    "red warning",
]

EDITORIAL_RUBRIC = {
    "impact": [
        "billion",
        "million",
        "nationwide",
        "global",
        "statewide",
        "record",
        "largest",
        "major",
        "multiple",
        "war",
        "attack",
        "red alert",
        "data leak",
        "antitrust",
    ],
    "decision_relevance": [
        "policy",
        "regulation",
        "budget",
        "rates",
        "tax",
        "market",
        "security",
        "privacy",
        "cyber",
        "infrastructure",
        "rain",
        "flood",
        "port",
        "bank",
        "recruitment",
        "approval",
    ],
    "novelty": [
        "first",
        "new",
        "launches",
        "launched",
        "rolls out",
        "released",
        "releases",
        "approves",
        "approved",
        "orders",
        "ordered",
        "rules",
        "ruled",
        "blocks",
        "blocked",
        "fines",
        "fined",
        "investigating",
        "probe launched",
        "announces",
        "announced",
    ],
    "consequence": [
        "approved",
        "ordered",
        "ruled",
        "blocked",
        "fined",
        "banned",
        "launched",
        "released",
        "cut",
        "raised",
        "resumed",
        "halted",
        "arrested",
        "seized",
        "red alert",
        "data leak",
        "antitrust",
    ],
    "source_authority": [
        "supreme court",
        "high court",
        "rbi",
        "sebi",
        "reserve bank",
        "regulator",
        "ministry",
        "government",
        "court",
        "police",
        "district administration",
        "european commission",
    ],
    "local_relevance": [
        "india",
        "indian",
        "mangaluru",
        "mangalore",
        "dakshina kannada",
        "coastal karnataka",
        "udupi",
        "kasaragod",
        "bantwal",
        "vittal",
    ],
}


@dataclass(frozen=True)
class Story:
    title: str
    link: str
    source: str
    bucket: str
    published: datetime
    summary: str
    image_url: str
    quality: int
    score: int
    reason: str
    editorial_class: str


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_url(url: str, timeout: int = 12, attempts: int = 2) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CodexDailyNewsBrief/1.0 (+local personal briefing)",
            "Accept": "application/rss+xml, application/xml, application/json, text/xml;q=0.9, */*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == attempts:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else attempt * 4
            delay = min(delay, 10)
            time.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                raise
            time.sleep(attempt * 2)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def parse_datetime(value: str | None, fallback_tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=fallback_tz)
        return parsed.astimezone(fallback_tz)
    except (TypeError, ValueError):
        pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M:%S",
        "%d %b, %Y %z",
        "%d %b %Y %z",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(fallback_tz)
        except ValueError:
            continue
    return None


def text_from_child(node: ElementTree.Element, child_name: str) -> str:
    child = node.find(child_name)
    if child is not None and child.text:
        return clean_text(child.text)
    return ""


def text_from_local_child(node: ElementTree.Element, child_names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in child_names}
    for child in list(node):
        if node_local_name(child) in wanted and child.text:
            return clean_text(child.text)
    return ""


def node_local_name(node: ElementTree.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()


def first_attr(node: ElementTree.Element, names: tuple[str, ...]) -> str:
    for name in names:
        value = node.attrib.get(name)
        if value:
            return clean_text(value)
    return ""


def image_from_rss_item(item: ElementTree.Element, summary: str) -> str:
    for child in item.iter():
        local_name = node_local_name(child)
        if local_name in {"content", "thumbnail"}:
            image_url = first_attr(child, ("url", "href"))
            medium = child.attrib.get("medium", "").lower()
            content_type = child.attrib.get("type", "").lower()
            if image_url and (local_name == "thumbnail" or medium == "image" or content_type.startswith("image/")):
                return image_url
        if local_name == "enclosure":
            image_url = first_attr(child, ("url", "href"))
            content_type = child.attrib.get("type", "").lower()
            if image_url and content_type.startswith("image/"):
                return image_url

    image_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary or "", re.IGNORECASE)
    return clean_text(image_match.group(1)) if image_match else ""


def image_from_html_meta(page: str) -> str:
    patterns = (
        r'<meta\s+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta\s+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return ""


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    value = repair_mojibake(value)
    value = re.sub(r"[\u200b-\u200f\u2060-\u206f]", "", value)
    value = value.translate(
        str.maketrans(
            {
                "‘": "'",
                "’": "'",
                "“": '"',
                "”": '"',
                "–": "-",
                "—": "-",
                "…": "...",
                "€": "EUR",
                "₹": "Rs",
            }
        )
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def strip_title_source_suffix(value: str) -> str:
    value = clean_text(value)
    if " - " not in value:
        return value
    title_part, suffix = value.rsplit(" - ", 1)
    suffix_words = suffix.split()
    if 1 <= len(suffix_words) <= 7:
        return title_part.strip()
    return value


def repair_mojibake(value: str) -> str:
    markers = ("â", "Â", "Ã")
    if not any(marker in value for marker in markers):
        return value
    try:
        repaired = value.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore")
    except UnicodeError:
        return value
    return repaired if repaired.strip() else value


def normalize_title(title: str) -> str:
    title = strip_title_source_suffix(title)
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    stopwords = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "with", "as", "by"}
    return " ".join(word for word in title.split() if word not in stopwords)


def title_tokens(title: str) -> set[str]:
    tokens = set(normalize_title(title).split())
    weak = {
        "news",
        "report",
        "reports",
        "says",
        "said",
        "new",
        "latest",
        "update",
        "updates",
        "company",
        "companies",
        "business",
    }
    return {token for token in tokens if len(token) > 2 and token not in weak}


def money_markers(title: str) -> set[str]:
    title = title.lower()
    return set(re.findall(r"\b\d+(?:\.\d+)?\s*(?:billion|million|crore|trillion)\b", title))


def entity_markers(title: str) -> set[str]:
    title = title.lower()
    entities = {
        "microsoft",
        "google",
        "openai",
        "apple",
        "nvidia",
        "meta",
        "tesla",
        "rbi",
        "sebi",
        "supreme court",
        "european commission",
        "ukraine",
        "russia",
        "tata",
    }
    return {entity for entity in entities if entity in title}


def titles_are_similar(left: str, right: str) -> bool:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    intersection = left_tokens & right_tokens
    overlap = len(intersection) / max(1, min(len(left_tokens), len(right_tokens)))
    jaccard = len(intersection) / max(1, len(left_tokens | right_tokens))
    shared_entities = entity_markers(left) & entity_markers(right)
    shared_money = money_markers(left) & money_markers(right)
    if shared_entities and shared_money and len(intersection) >= 2:
        return True
    if {"microsoft", "ai"} <= left_tokens and {"microsoft", "ai"} <= right_tokens and "frontier" in intersection:
        return True
    return overlap >= 0.72 or (jaccard >= 0.52 and len(intersection) >= 4)


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def classify_story(title: str, summary: str, bucket: str) -> str:
    haystack = f"{title} {summary}".lower()
    if any(keyword_matches(haystack, term) for term in EDITORIAL_CLASS_RULES["commercial"]):
        return "commercial"
    if any(keyword_matches(haystack, term) for term in EDITORIAL_CLASS_RULES["culture_lite"]):
        return "culture_lite"
    if any(keyword_matches(haystack, term) for term in EDITORIAL_CLASS_RULES["civic_safety"]):
        return "civic_safety"
    if any(keyword_matches(haystack, term) for term in EDITORIAL_CLASS_RULES["high_impact"]):
        return "high_impact"
    if bucket == "local":
        return "local_community"
    return "general"


def canonical_link(link: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(link)
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        keep_params = {"newsID", "id", "article", "storyid", "oc"}
        kept_query = urllib.parse.urlencode(
            [(key, value) for key, value in query_pairs if key in keep_params]
        )
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), kept_query, "")
        )
    except ValueError:
        return link.strip()


def clean_link(link: str) -> str:
    link = clean_text(link)
    duplicate_prefixes = (
        "https://www.sebi.gov.in/https://www.sebi.gov.in/",
        "http://www.sebi.gov.in/http://www.sebi.gov.in/",
    )
    for prefix in duplicate_prefixes:
        if link.startswith(prefix):
            return link.replace(prefix, prefix.split("/", 3)[0] + "//" + prefix.split("/")[2] + "/", 1)
    return link


def source_allows_item(source: dict[str, Any], title: str, summary: str) -> bool:
    haystack = f"{title} {summary}".lower()
    required_terms = source.get("require_any_terms", [])
    if required_terms and not any(keyword_matches(haystack, term) for term in required_terms):
        return False
    excluded_terms = source.get("exclude_terms", [])
    if excluded_terms and any(keyword_matches(haystack, term) for term in excluded_terms):
        return False
    return True


def rss_item_source_name(item: ElementTree.Element, fallback: str) -> str:
    source = text_from_child(item, "source") or text_from_local_child(item, ("source",))
    return source or fallback


def source_allows_publisher(source: dict[str, Any], publisher: str) -> bool:
    publisher_key = publisher.lower()
    allowed_publishers = [name.lower() for name in source.get("allowed_item_sources", [])]
    if allowed_publishers and not any(name in publisher_key for name in allowed_publishers):
        return False
    blocked_publishers = [name.lower() for name in source.get("exclude_item_sources", [])]
    if blocked_publishers and any(name in publisher_key for name in blocked_publishers):
        return False
    return True


def apply_source_bonus(score: int, reason: str, source: dict[str, Any]) -> tuple[int, str]:
    bonus = int(source.get("score_bonus", 0))
    if not bonus:
        return score, reason
    sign = "+" if bonus > 0 else ""
    return score + bonus, f"{reason}; source_bonus {sign}{bonus}"


def editorial_rubric_score(
    title: str,
    summary: str,
    bucket: str,
    quality: int,
    age_hours: float,
) -> tuple[int, list[str]]:
    haystack = f"{title} {summary}".lower()
    points = 0
    labels: list[str] = []
    weights = {
        "impact": 4,
        "decision_relevance": 4,
        "novelty": 3,
        "consequence": 4,
        "source_authority": 3,
        "local_relevance": 3,
    }
    for axis, terms in EDITORIAL_RUBRIC.items():
        if any(keyword_matches(haystack, term) for term in terms):
            points += weights[axis]
            labels.append(axis.replace("_", " "))

    if quality >= 5 and "source authority" not in labels:
        points += weights["source_authority"]
        labels.append("source authority")
    elif quality >= 4 and any(label in labels for label in ("consequence", "decision relevance", "impact")):
        points += 1

    if age_hours <= 12 and any(label in labels for label in ("novelty", "consequence")):
        points += 2
        labels.append("fresh change")

    if bucket == "local" and "local relevance" in labels:
        points += 2
        labels.append("local consequence")
    if bucket == "tech" and any(keyword_matches(haystack, term) for term in ("ai", "cyber", "security", "chip", "model", "privacy")):
        points += 2
        labels.append("professional signal")
    return min(points, 18), labels[:5]


def story_rubric_labels(story: Story) -> list[str]:
    labels: list[str] = []
    for part in story.reason.split(";"):
        part = part.strip()
        if part.startswith("rubric "):
            labels.extend(label.strip() for label in part.removeprefix("rubric ").split(",") if label.strip())
    return labels[:4]


def impact_tier(story: Story) -> str:
    if story.score >= 28:
        return "High impact"
    if story.score >= 22:
        return "Material"
    if story.score >= 17:
        return "Worth tracking"
    return "Context"


def is_breaking_candidate(story: Story, now: datetime) -> bool:
    haystack = f"{story.title} {story.summary}".lower()
    age_hours = max(0.0, (now - story.published).total_seconds() / 3600)
    if age_hours > 18:
        return False
    if story.editorial_class in TOP_EXCLUDED_CLASSES:
        return False
    if not any(keyword_matches(haystack, term) for term in BREAKING_TERMS):
        return False
    if story.score < 26 and story.editorial_class != "high_impact":
        return False
    return True


def sentence(value: str, max_words: int = 28) -> str:
    value = clean_text(value)
    if not value:
        return ""
    first = re.split(r"(?<=[.!?])\s+", value)[0]
    words = first.split()
    if len(words) > max_words:
        first = " ".join(words[:max_words]).rstrip(",;:") + "."
    if first and first[-1] not in ".!?":
        first += "."
    return first


def headline(value: str, max_words: int = 13) -> str:
    value = strip_title_source_suffix(value)
    value = re.sub(r"\s*[-|]\s*(BBC News|The Hindu|Times of India|Mangalore Today).*$", "", value)
    words = value.split()
    if len(words) > max_words:
        value = " ".join(words[:max_words]).rstrip(",;:") + "..."
    return value


def score_item(
    title: str,
    summary: str,
    bucket: str,
    quality: int,
    settings: dict[str, Any],
    now: datetime,
    published: datetime,
) -> tuple[int, str]:
    haystack = f"{title} {summary}".lower()
    editorial_class = classify_story(title, summary, bucket)
    score = quality * 2
    reasons: list[str] = [f"source {quality}/5", editorial_class]

    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    if age_hours <= 12:
        score += 5
        reasons.append("fresh")
    elif age_hours <= 24:
        score += 3
    else:
        score += 1

    matched_keywords = [word for word in BUCKET_KEYWORDS.get(bucket, []) if keyword_matches(haystack, word)]
    if matched_keywords:
        score += min(8, len(matched_keywords) * 2)
        reasons.append(f"matches {', '.join(matched_keywords[:3])}")

    outcome_terms = settings.get("outcome_terms", [])
    if any(term in haystack for term in outcome_terms):
        score += 6
        reasons.append("outcome")

    impact_terms = ["million", "billion", "nationwide", "global", "supreme court", "rbi", "government", "regulator"]
    if any(term in haystack for term in impact_terms):
        score += 3
        reasons.append("impact")

    rubric_points, rubric_labels = editorial_rubric_score(title, summary, bucket, quality, age_hours)
    if rubric_points:
        score += rubric_points
        reasons.append(f"rubric {', '.join(rubric_labels)}")

    individual_crime_terms = ("gangrape", "sexual assault", "robbery", "murder", "domestic violence")
    if bucket != "local" and any(term in haystack for term in individual_crime_terms):
        score -= 6
        reasons.append("individual crime -6")

    class_adjustments = {
        "high_impact": 5,
        "civic_safety": 4,
        "general": 0,
        "local_community": -2,
        "culture_lite": -7,
        "commercial": -12,
    }
    adjustment = class_adjustments.get(editorial_class, 0)
    score += adjustment
    if adjustment:
        reasons.append(f"class {adjustment:+d}")

    return score, "; ".join(reasons)


def is_noise(title: str, summary: str, settings: dict[str, Any]) -> bool:
    haystack = f"{title} {summary}".lower()
    if re.search(r"\blive\b", title.lower()):
        return True
    return any(term in haystack for term in settings.get("noise_terms", []))


def has_bucket_relevance(title: str, summary: str, bucket: str) -> bool:
    if bucket != "local":
        return True
    haystack = f"{title} {summary}".lower()
    for publisher_name in ("mangalore today", "mangalorean.com", "coastal digest"):
        haystack = haystack.replace(publisher_name, "")
    return any(keyword_matches(haystack, term) for term in BUCKET_KEYWORDS["local"])


def parse_daijiworld_datetime(value: str, fallback_tz: ZoneInfo) -> datetime | None:
    value = clean_text(value)
    value = re.sub(r"^[A-Za-z]{3},\s+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    for fmt in ("%b %d %Y %I:%M:%S %p", "%b %d %Y %I:%M %p"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=fallback_tz)
        except ValueError:
            continue
    return parse_datetime(value, fallback_tz)


def collect_rss(source: dict[str, Any], settings: dict[str, Any], now: datetime, since: datetime) -> list[Story]:
    try:
        payload = fetch_url(source["url"])
        root = ElementTree.fromstring(payload)
    except Exception as exc:  # network and XML failures should not kill the full brief
        print(f"WARN: {source['name']} failed: {exc}", file=sys.stderr)
        return []

    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    stories: list[Story] = []
    tz = ZoneInfo(settings["timezone"])
    for item in items:
        title = text_from_child(item, "title") or text_from_child(item, "{http://www.w3.org/2005/Atom}title")
        link = text_from_child(item, "link")
        if not link:
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.attrib.get("href", "") if atom_link is not None else ""
        link = clean_link(link)
        summary = (
            text_from_child(item, "description")
            or text_from_child(item, "summary")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}summary")
            or text_from_local_child(item, ("description", "summary", "content", "encoded"))
        )
        published = parse_datetime(
            text_from_child(item, "pubDate")
            or text_from_child(item, "published")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}published")
            or text_from_child(item, "updated")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}updated")
            or text_from_local_child(item, ("pubDate", "published", "updated", "date")),
            tz,
        )
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, summary, settings):
            continue
        if not source_allows_item(source, title, summary):
            continue
        if not has_bucket_relevance(title, summary, source["bucket"]):
            continue
        story_source = rss_item_source_name(item, source["name"]) if source.get("use_item_source") else source["name"]
        if not source_allows_publisher(source, story_source):
            continue
        image_url = image_from_rss_item(item, summary)
        score, reason = score_item(title, summary, source["bucket"], int(source["quality"]), settings, now, published)
        score, reason = apply_source_bonus(score, reason, source)
        editorial_class = classify_story(title, summary, source["bucket"])
        stories.append(
            Story(
                title=title,
                link=link,
                source=story_source,
                bucket=source["bucket"],
                published=published,
                summary=summary,
                image_url=image_url,
                quality=int(source["quality"]),
                score=score,
                reason=reason,
                editorial_class=editorial_class,
            )
        )
    return stories


def collect_daijiworld(source: dict[str, Any], settings: dict[str, Any], now: datetime, since: datetime) -> list[Story]:
    try:
        payload = fetch_url(source["url"], attempts=1)
    except Exception as exc:
        print(f"WARN: {source['name']} failed: {exc}", file=sys.stderr)
        return []

    page = payload.decode("utf-8", errors="replace")
    item_pattern = re.compile(
        r'<h2>\s*<a\s+href="(?P<link>https://www\.daijiworld\.com/news/newsDisplay\?newsID=\d+)">'
        r"(?P<title>.*?)</a>\s*</h2>.*?"
        r'<i class="fa fa-calendar">\s*&nbsp;\s*</i>\s*(?P<date>[^<]+)',
        re.IGNORECASE | re.DOTALL,
    )
    tz = ZoneInfo(settings["timezone"])
    stories: list[Story] = []
    image_fetches = 0

    for match in item_pattern.finditer(page):
        title = clean_text(match.group("title"))
        link = clean_link(match.group("link"))
        published = parse_daijiworld_datetime(match.group("date"), tz)
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, "", settings):
            continue
        if not source_allows_item(source, title, ""):
            continue
        if not has_bucket_relevance(title, "", source["bucket"]):
            continue
        image_url = ""
        if image_fetches < 8:
            try:
                article_page = fetch_url(link, timeout=8, attempts=1).decode("utf-8", errors="replace")
                image_url = image_from_html_meta(article_page)
                image_fetches += 1
            except Exception:
                image_url = ""
        score, reason = score_item(title, "", source["bucket"], int(source["quality"]), settings, now, published)
        score, reason = apply_source_bonus(score, reason, source)
        editorial_class = classify_story(title, "", source["bucket"])
        stories.append(
            Story(
                title=title,
                link=link,
                source=source["name"],
                bucket=source["bucket"],
                published=published,
                summary="Daijiworld local listing item; open the source for full context.",
                image_url=image_url,
                quality=int(source["quality"]),
                score=score,
                reason=reason,
                editorial_class=editorial_class,
            )
        )
    return stories


def gdelt_url(query: str, hours: int) -> str:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": "30",
        "sort": "HybridRel",
        "timespan": f"{hours}h",
    }
    return "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)


def collect_gdelt(source: dict[str, Any], settings: dict[str, Any], now: datetime, since: datetime, hours: int) -> list[Story]:
    try:
        payload = fetch_url(gdelt_url(source["query"], hours), attempts=1)
        data = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        print(f"WARN: {source['name']} failed: {exc}", file=sys.stderr)
        return []

    tz = ZoneInfo(settings["timezone"])
    stories: list[Story] = []
    for article in data.get("articles", []):
        title = clean_text(article.get("title", ""))
        link = clean_link(article.get("url", ""))
        summary = clean_text(article.get("seendate", ""))
        published = parse_datetime(article.get("seendate"), tz)
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, summary, settings):
            continue
        if not source_allows_item(source, title, summary):
            continue
        if not has_bucket_relevance(title, summary, source["bucket"]):
            continue
        domain = article.get("domain") or urllib.parse.urlsplit(link).netloc
        source_name = f"{source['name']} / {domain}"
        score, reason = score_item(title, summary, source["bucket"], int(source["quality"]), settings, now, published)
        score, reason = apply_source_bonus(score, reason, source)
        editorial_class = classify_story(title, summary, source["bucket"])
        stories.append(
            Story(
                title=title,
                link=link,
                source=source_name,
                bucket=source["bucket"],
                published=published,
                summary="Recent coverage surfaced by GDELT; open the source for full context.",
                image_url=clean_text(article.get("socialimage", "")),
                quality=int(source["quality"]),
                score=score,
                reason=reason,
                editorial_class=editorial_class,
            )
        )
    return stories


def dedupe(stories: list[Story]) -> list[Story]:
    best_by_key: dict[str, Story] = {}
    for story in stories:
        title_key = normalize_title(story.title)
        title_tokens = title_key.split()
        compact_title = " ".join(title_tokens[:10])
        key = canonical_link(story.link) or compact_title
        if not key:
            continue

        existing = best_by_key.get(key)
        if existing is None or story.score > existing.score:
            best_by_key[key] = story
            continue

        loose_key = compact_title
        if loose_key:
            loose_existing = best_by_key.get(loose_key)
            if loose_existing is None or story.score > loose_existing.score:
                best_by_key[loose_key] = story

    unique = list({canonical_link(story.link): story for story in best_by_key.values()}.values())
    ranked = sorted(unique, key=lambda item: (item.score, item.published), reverse=True)
    fuzzy_unique: list[Story] = []
    for story in ranked:
        if any(titles_are_similar(story.title, selected.title) for selected in fuzzy_unique):
            continue
        fuzzy_unique.append(story)
    return fuzzy_unique


def collect(settings: dict[str, Any], sources: list[dict[str, Any]], hours: int) -> list[Story]:
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    since = now - timedelta(hours=hours)
    stories: list[Story] = []
    for source in sources:
        if source.get("enabled") is False:
            continue
        if source["type"] == "rss":
            stories.extend(collect_rss(source, settings, now, since))
        elif source["type"] == "daijiworld_html":
            stories.extend(collect_daijiworld(source, settings, now, since))
        elif source["type"] == "gdelt":
            time.sleep(0.5)
            stories.extend(collect_gdelt(source, settings, now, since, hours))
        else:
            print(f"WARN: unsupported source type {source['type']} for {source['name']}", file=sys.stderr)
    return dedupe(stories)


def select_sections(stories: list[Story], settings: dict[str, Any]) -> dict[str, list[Story]]:
    max_items = settings["max_items"]
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    sections: dict[str, list[Story]] = {}
    sections["breaking"] = [
        story
        for story in stories
        if is_breaking_candidate(story, now)
    ][: int(max_items.get("breaking", 4))]

    for bucket in ("global", "india", "tech", "local"):
        min_score = SECTION_MIN_SCORE[bucket]
        bucket_stories = [
            story
            for story in stories
            if story.bucket == bucket and story.score >= min_score
        ]
        if bucket == "local":
            bucket_stories = sorted(
                bucket_stories,
                key=lambda story: (
                    story.editorial_class in {"civic_safety", "high_impact"},
                    story.editorial_class not in {"commercial", "culture_lite"},
                    story.score,
                    story.published,
                ),
                reverse=True,
            )
        sections[bucket] = bucket_stories[: int(max_items[bucket])]

    seen_links = {canonical_link(story.link) for bucket in sections.values() for story in bucket}
    top_pool = [
        story
        for story in stories
        if story.score >= 18
        and story.editorial_class not in TOP_EXCLUDED_CLASSES
        and (canonical_link(story.link) not in seen_links or story.score >= 22)
    ]
    top_pool = sorted(
        top_pool,
        key=lambda story: (
            story.editorial_class == "high_impact",
            story.editorial_class == "civic_safety",
            story.bucket != "local",
            story.score,
            story.published,
        ),
        reverse=True,
    )
    top_items: list[Story] = []
    bucket_counts = {bucket: 0 for bucket in ("global", "india", "tech", "local")}
    top_limit = int(max_items["top"])
    for story in top_pool:
        bucket_cap = 2 if story.bucket in {"india", "tech", "global"} else 1
        if bucket_counts.get(story.bucket, 0) >= bucket_cap:
            continue
        top_items.append(story)
        bucket_counts[story.bucket] = bucket_counts.get(story.bucket, 0) + 1
        if len(top_items) >= top_limit:
            break
    if len(top_items) < top_limit:
        selected_top_links = {canonical_link(story.link) for story in top_items}
        for story in top_pool:
            if canonical_link(story.link) in selected_top_links:
                continue
            top_items.append(story)
            if len(top_items) >= top_limit:
                break
    sections["top"] = top_items

    selected = {canonical_link(story.link) for bucket in sections.values() for story in bucket}
    sections["watchlist"] = [
        story
        for story in stories
        if canonical_link(story.link) not in selected
        and story.score >= 16
        and story.editorial_class not in WATCHLIST_EXCLUDED_CLASSES
    ][: int(max_items["watchlist"])]
    return sections


def story_line(story: Story, tz: ZoneInfo) -> str:
    summary = sentence(story.summary) or "Open the source for the full update."
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    return (
        f"**{headline(story.title)}**\n"
        f"{summary} "
        f"[Source: {story.source}]({story.link}) "
        f"`{timestamp}; score {story.score}; {story.editorial_class}`"
    )


def render_markdown(sections: dict[str, list[Story]], settings: dict[str, Any], generated_at: datetime) -> str:
    lines: list[str] = []
    labels = settings["section_labels"]
    lines.append(f"Briefing generated: {generated_at.strftime('%A, %d %B %Y, %H:%M IST')}")
    lines.append("")
    lines.append("TODAY'S TOP 5")
    if sections["top"]:
        for index, story in enumerate(sections["top"], start=1):
            lines.append(f"{index}. {headline(story.title, 12)}")
    else:
        lines.append("No high-confidence top stories found in the configured window.")
    lines.append("")
    lines.append("---")

    lines.append("")
    lines.append("BREAKING WATCH")
    lines.append("")
    if sections.get("breaking"):
        for story in sections["breaking"]:
            lines.append(story_line(story, generated_at.tzinfo or ZoneInfo(settings["timezone"])))
            lines.append("")
    else:
        lines.append("No critical breaking items crossed the threshold.")
        lines.append("")
    lines.append("---")

    for bucket in ("global", "india", "tech", "local"):
        lines.append("")
        lines.append(labels[bucket])
        lines.append("")
        if sections[bucket]:
            for story in sections[bucket]:
                lines.append(story_line(story, generated_at.tzinfo or ZoneInfo(settings["timezone"])))
                lines.append("")
        else:
            lines.append("No strong, recent items found for this section.")
            lines.append("")
        lines.append("---")

    lines.append("")
    lines.append("WATCHLIST")
    lines.append("")
    if sections["watchlist"]:
        for story in sections["watchlist"]:
            lines.append(story_line(story, generated_at.tzinfo or ZoneInfo(settings["timezone"])))
            lines.append("")
    else:
        lines.append("No additional watchlist items crossed the threshold.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def markdown_to_html(markdown: str, title: str) -> str:
    body_lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            body_lines.append("")
        elif line == "---":
            body_lines.append("<hr>")
        elif line.isupper() or line.startswith("Briefing generated:"):
            tag = "h1" if line.startswith("Briefing generated:") else "h2"
            body_lines.append(f"<{tag}>{html.escape(line)}</{tag}>")
        elif re.match(r"^\d+\. ", line):
            body_lines.append(f"<p class=\"top\">{html.escape(line)}</p>")
        else:
            line = html.escape(line)
            line = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"\[Source: ([^\]]+)\]\(([^)]+)\)", r'<a href="\2">Source: \1</a>', line)
            line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
            body_lines.append(f"<p>{line}</p>")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f4;
      --text: #17201c;
      --muted: #59615d;
      --rule: #d7dcd5;
      --accent: #0b6b57;
      --paper: #ffffff;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111715;
        --text: #eef4f0;
        --muted: #aab5af;
        --rule: #2c3934;
        --accent: #58c7ab;
        --paper: #17201c;
      }}
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.55 "Segoe UI", system-ui, sans-serif;
    }}
    main {{
      max-width: 860px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    h1 {{
      font-size: 16px;
      font-weight: 600;
      color: var(--muted);
      margin: 0 0 22px;
    }}
    h2 {{
      font-size: 18px;
      margin: 28px 0 12px;
      letter-spacing: 0;
    }}
    p {{
      margin: 10px 0;
      background: var(--paper);
      padding: 12px 14px;
      border-left: 3px solid var(--rule);
    }}
    p.top {{
      border-left-color: var(--accent);
      font-weight: 600;
    }}
    a {{
      color: var(--accent);
      text-decoration-thickness: 1px;
    }}
    code {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    hr {{
      border: 0;
      border-top: 1px solid var(--rule);
      margin: 24px 0;
    }}
  </style>
</head>
<body>
  <main>
    {"".join(body_lines)}
  </main>
</body>
</html>
"""


def section_deck(bucket: str) -> str:
    decks = {
        "global": "High-signal world developments with policy, security, markets, science, and climate consequences.",
        "india": "National decisions, institutions, infrastructure, economy, regulation, and court outcomes.",
        "tech": "AI, platforms, chips, cybersecurity, funding, product shifts, and technology regulation.",
        "local": "Mangalore, coastal Karnataka, Udupi, Kasaragod, and nearby civic or regional updates.",
        "watchlist": "Secondary but credible signals worth a quick scan after the main brief.",
    }
    return decks.get(bucket, "")


def section_short_label(bucket: str) -> str:
    return {
        "global": "GLOBAL",
        "india": "INDIA",
        "tech": "TECH",
        "local": "COASTAL",
    }.get(bucket, "BRIEF")


def html_story_card(
    story: Story,
    tz: ZoneInfo,
    rank: int | None = None,
    compact: bool = False,
    lead: bool = False,
) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 16 if compact else 18))
    summary = html.escape(sentence(story.summary, 24 if compact else 30) or "Open the source for the full update.")
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    image_url = html.escape(story.image_url, quote=True)
    rank_html = f'<span class="rank">{rank:02d}</span>' if rank is not None else ""
    tier = impact_tier(story)
    tier_slug = tier.lower().replace(" ", "-")
    rubric_labels = story_rubric_labels(story)
    rubric_html = "".join(f"<span>{html.escape(label.title())}</span>" for label in rubric_labels)
    class_name = "story compact" if compact else "story"
    class_name += " lead" if lead else ""
    class_name += f" {tier_slug}"
    fallback_label = html.escape(section_short_label(story.bucket))
    image_html = (
        f'<div class="story-media"><span>{fallback_label}</span><img src="{image_url}" alt="" loading="lazy" onerror="this.remove(); this.parentElement.classList.add(\'fallback\');"></div>'
        if image_url
        else f'<div class="story-media fallback"><span>{fallback_label}</span></div>'
    )
    return f"""
      <article class="{class_name}">
        <a class="story-link" href="{url}">
          {image_html}
          <div class="story-kicker">{rank_html}<span>{source}</span><span>{timestamp}</span></div>
          <h3>{title}</h3>
          <p>{summary}</p>
          <div class="rubric">{rubric_html or f"<span>{html.escape(story.editorial_class.replace('_', ' ').title())}</span>"}</div>
          <div class="story-meta"><span>{html.escape(tier)}</span><span>Open source</span></div>
        </a>
      </article>
    """


def html_top_story(story: Story, tz: ZoneInfo, rank: int, lead: bool = False) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 16 if lead else 13))
    summary = html.escape(sentence(story.summary, 28 if lead else 18) or "Open the source for the full update.")
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    image_url = html.escape(story.image_url, quote=True)
    tier = html.escape(impact_tier(story))
    rubric_labels = story_rubric_labels(story)
    rubric_html = "".join(f"<span>{html.escape(label.title())}</span>" for label in rubric_labels[:4])
    class_name = "top-item lead-top" if lead else "top-item"
    fallback_label = html.escape(section_short_label(story.bucket))
    image_html = (
        f'<div class="top-media"><span>{fallback_label}</span><img src="{image_url}" alt="" loading="lazy" onerror="this.remove(); this.parentElement.classList.add(\'fallback\');"></div>'
        if image_url
        else f'<div class="top-media fallback"><span>{fallback_label}</span></div>'
    )
    return f"""
      <article class="{class_name}">
        <a href="{url}">
          {image_html}
          <div class="top-rule"><span>{rank:02d}</span><span>{tier}</span></div>
          <h3>{title}</h3>
          <p>{summary}</p>
          <div class="rubric">{rubric_html}</div>
          <div class="story-meta"><span>{source}</span><span>{timestamp}</span></div>
        </a>
      </article>
    """


def html_rail_item(story: Story, tz: ZoneInfo, label: str | None = None) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 11))
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    kicker = html.escape(label or section_short_label(story.bucket))
    return f"""
      <a class="rail-item" href="{url}">
        <span>{kicker}</span>
        <strong>{title}</strong>
        <small>{source} / {timestamp}</small>
      </a>
    """


def html_hero_story(story: Story, tz: ZoneInfo) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 15))
    summary = html.escape(sentence(story.summary, 28) or "Open the source for the full update.")
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    image_url = html.escape(story.image_url, quote=True)
    fallback_label = html.escape(section_short_label(story.bucket))
    image_html = (
        f'<img src="{image_url}" alt="" loading="eager" onerror="this.remove(); this.parentElement.classList.add(\'fallback\');">'
        if image_url
        else ""
    )
    return f"""
      <article class="hero-story">
        <a href="{url}">
          <div class="hero-media">
            <span>{fallback_label}</span>
            {image_html}
          </div>
          <div class="hero-copy">
            <div class="hero-kicker"><span>{html.escape(section_short_label(story.bucket))}</span><small>{source} / {timestamp}</small></div>
            <h2>{title}</h2>
            <p>{summary}</p>
          </div>
        </a>
      </article>
    """


def html_popular_item(story: Story, tz: ZoneInfo) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 12))
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    image_url = html.escape(story.image_url, quote=True)
    fallback_label = html.escape(section_short_label(story.bucket))
    image_html = (
        f'<div class="popular-thumb"><span>{fallback_label}</span><img src="{image_url}" alt="" loading="lazy" onerror="this.remove(); this.parentElement.classList.add(\'fallback\');"></div>'
        if image_url
        else f'<div class="popular-thumb fallback"><span>{fallback_label}</span></div>'
    )
    return f"""
      <a class="popular-item" href="{url}">
        {image_html}
        <div>
          <span>{html.escape(section_short_label(story.bucket))}</span>
          <strong>{title}</strong>
          <small>{source} / {timestamp}</small>
        </div>
      </a>
    """


def html_breaking_card(story: Story, tz: ZoneInfo) -> str:
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    title = html.escape(headline(story.title, 12))
    source = html.escape(story.source)
    url = html.escape(story.link, quote=True)
    image_url = html.escape(story.image_url, quote=True)
    fallback_label = html.escape(section_short_label(story.bucket))
    image_html = (
        f'<div class="breaking-media"><span>{fallback_label}</span><img src="{image_url}" alt="" loading="lazy" onerror="this.remove(); this.parentElement.classList.add(\'fallback\');"></div>'
        if image_url
        else f'<div class="breaking-media fallback"><span>{fallback_label}</span></div>'
    )
    return f"""
      <article class="breaking-card">
        <a href="{url}">
          {image_html}
          <div class="breaking-copy">
            <span>{html.escape(impact_tier(story))}</span>
            <strong>{title}</strong>
            <small>{source} / {timestamp}</small>
          </div>
        </a>
      </article>
    """


def render_html_brief(sections: dict[str, list[Story]], settings: dict[str, Any], generated_at: datetime) -> str:
    tz = generated_at.tzinfo or ZoneInfo(settings["timezone"])
    labels = settings["section_labels"]
    selected_without_top = [story for bucket in ("global", "india", "tech", "local", "watchlist") for story in sections[bucket]]
    total_count = len(selected_without_top)
    strongest = max(selected_without_top or sections["top"], key=lambda story: story.score, default=None)
    strongest_label = impact_tier(strongest) if strongest else "No signal"
    section_stats = "".join(
        f'<div><strong>{len(sections[bucket])}</strong><span>{html.escape(labels[bucket].title())}</span></div>'
        for bucket in ("global", "india", "tech", "local")
    )
    lead_story = sections["top"][0] if sections["top"] else strongest
    fresh_pool = [story for story in sections["top"][1:] + sections["india"] + sections["global"] if story is not lead_story]
    popular_pool = [
        story
        for story in sorted(selected_without_top, key=lambda item: item.score, reverse=True)
        if story is not lead_story
    ]
    fresh_rail = "\n".join(html_rail_item(story, tz) for story in fresh_pool[:6])
    popular_rail = "\n".join(html_popular_item(story, tz) for story in popular_pool[:5])
    hero_html = html_hero_story(lead_story, tz) if lead_story else '<p class="empty">No high-confidence lead story found.</p>'
    breaking_cards = "\n".join(html_breaking_card(story, tz) for story in sections.get("breaking", []))
    breaking_html = (
        f"""
        <section id="breaking" class="breaking-gallery" aria-label="Breaking watch">
          <div class="module-head social-head">
            <span>Breaking</span>
            <strong>Critical signals since the morning edition</strong>
          </div>
          <div class="breaking-grid">{breaking_cards}</div>
        </section>
        """
        if breaking_cards
        else ""
    )

    top_cards = "\n".join(
        html_top_story(story, tz, index, lead=(index == 1))
        for index, story in enumerate(sections["top"], start=1)
    )
    if not top_cards:
        top_cards = '<p class="empty">No high-confidence top stories found in the configured window.</p>'
    section_blocks: list[str] = []
    for bucket in ("global", "india", "tech", "local"):
        stories = sections[bucket]
        cards = "\n".join(html_story_card(story, tz) for story in stories)
        if not cards:
            cards = '<p class="empty">No strong, recent items found for this section.</p>'
        section_blocks.append(
            f"""
            <section id="{bucket}" class="news-section section-{bucket}">
              <div class="section-head">
                <div>
                  <p class="eyebrow">{html.escape(labels[bucket])}</p>
                  <h2>{html.escape(labels[bucket].title())}</h2>
                </div>
                <p>{html.escape(section_deck(bucket))}</p>
              </div>
              <div class="story-grid">{cards}</div>
            </section>
            """
        )

    watchlist_cards = "\n".join(html_story_card(story, tz) for story in sections["watchlist"])
    if not watchlist_cards:
        watchlist_cards = '<p class="empty">No additional watchlist items crossed the threshold.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings["brief_title"])}</title>
  <script>
    (() => {{
      const saved = localStorage.getItem("newsbrief-theme");
      const systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      document.documentElement.dataset.theme = saved || (systemDark ? "dark" : "light");
    }})();
  </script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;650;800&family=Newsreader:opsz,wght@6..72,500;6..72,700;6..72,800&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f3ee;
      --surface: #ffffff;
      --surface-2: #efeee9;
      --ink: #121411;
      --muted: #626760;
      --faint: #ded8cd;
      --rule: #c9c0b2;
      --accent: #9b153f;
      --accent-2: #145c53;
      --gold: #a87624;
      --shadow: 0 18px 46px rgba(27, 22, 16, 0.08);
      --brand-red: #e22c2f;
      --grid-x: rgba(18,20,17,0.035);
      --grid-y: rgba(18,20,17,0.025);
      --nav-bg: rgba(246, 243, 238, 0.94);
      --soft-panel: rgba(255, 255, 255, 0.62);
      --warm-hover: #fffaf1;
      --body-copy: #313836;
      --section-copy: #4b514d;
    }}
    :root[data-theme="dark"] {{
      color-scheme: dark;
      --bg: #10110f;
      --surface: #181a17;
      --surface-2: #20221f;
      --ink: #f4efe6;
      --muted: #b9b0a4;
      --faint: #393831;
      --rule: #5a554b;
      --accent: #ff4a4d;
      --accent-2: #6bc3b2;
      --gold: #d6a44a;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.34);
      --brand-red: #ff3538;
      --grid-x: rgba(255,255,255,0.045);
      --grid-y: rgba(255,255,255,0.028);
      --nav-bg: rgba(16, 17, 15, 0.9);
      --soft-panel: rgba(24, 26, 23, 0.78);
      --warm-hover: #22241f;
      --body-copy: #ddd7cc;
      --section-copy: #cbc3b7;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background:
        linear-gradient(90deg, var(--grid-x) 1px, transparent 1px),
        linear-gradient(180deg, var(--grid-y) 1px, transparent 1px),
        var(--bg);
      background-size: 72px 72px, 72px 72px, auto;
      color: var(--ink);
      font: 15px/1.55 Inter, "Segoe UI", system-ui, sans-serif;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
      overflow-x: hidden;
    }}
    a {{ color: inherit; text-decoration: none; }}
    a:focus-visible {{
      outline: 3px solid rgba(155, 21, 63, 0.35);
      outline-offset: 3px;
    }}
    .page {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 22px 64px;
    }}
    .masthead {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: end;
      border-bottom: 2px solid var(--ink);
      padding-bottom: 22px;
    }}
    .brand {{
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: clamp(58px, 8vw, 106px);
      font-weight: 800;
      line-height: 0.82;
      letter-spacing: 0;
      margin: 0 0 12px;
    }}
    .dateline {{
      color: var(--muted);
      margin: 0;
      max-width: 720px;
    }}
    .issue-box {{
      min-width: 210px;
      border: 1px solid var(--ink);
      padding: 14px 16px;
      background: var(--surface);
      text-align: right;
    }}
    .mast-actions {{
      justify-self: end;
      display: grid;
      gap: 10px;
      align-items: end;
    }}
    .theme-toggle {{
      justify-self: end;
      border: 1px solid var(--faint);
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
      display: inline-grid;
      grid-template-columns: 28px auto;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      font: 800 10px/1 Inter, "Segoe UI", system-ui, sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .theme-toggle:hover {{
      border-color: var(--brand-red);
      color: var(--brand-red);
    }}
    .theme-toggle:focus-visible {{
      outline: 3px solid rgba(226, 44, 47, 0.32);
      outline-offset: 3px;
    }}
    .theme-icon {{
      position: relative;
      width: 28px;
      height: 15px;
      border: 1px solid currentColor;
      border-radius: 999px;
    }}
    .theme-icon::after {{
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: currentColor;
      transition: transform 160ms ease;
    }}
    :root[data-theme="dark"] .theme-icon::after {{
      transform: translateX(13px);
    }}
    .issue-box strong {{
      display: block;
      font-size: 26px;
      line-height: 1;
    }}
    .issue-box span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-top: 8px;
    }}
    .nav {{
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 12px 0;
      background: var(--nav-bg);
      border-bottom: 1px solid var(--faint);
      backdrop-filter: blur(10px);
    }}
    .nav a {{
      border: 1px solid var(--rule);
      padding: 7px 10px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      background: var(--surface);
    }}
    .nav a:hover {{
      background: var(--ink);
      color: var(--warm-hover);
      border-color: var(--ink);
    }}
    .editorial-strip {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      border: 1px solid var(--ink);
      border-top: 0;
      background: var(--soft-panel);
    }}
    .editorial-strip div {{
      min-height: 78px;
      padding: 13px 14px;
      border-right: 1px solid var(--rule);
      display: grid;
      align-content: space-between;
    }}
    .editorial-strip div:last-child {{
      border-right: 0;
    }}
    .editorial-strip strong {{
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 26px;
      line-height: 1;
    }}
    .editorial-strip span {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .front-page {{
      margin: 28px 0 34px;
      border-top: 3px solid var(--ink);
      border-bottom: 1px solid var(--ink);
      background: rgba(255, 255, 255, 0.38);
    }}
    .front-head {{
      display: flex;
      gap: 18px;
      align-items: baseline;
      justify-content: space-between;
      padding: 16px 0 12px;
      border-bottom: 1px solid var(--ink);
    }}
    .front-head h2 {{
      margin: 0;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: clamp(34px, 4vw, 54px);
      font-weight: 800;
      line-height: 0.98;
      letter-spacing: 0;
      text-align: right;
    }}
    .top-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      align-items: stretch;
      border-left: 1px solid var(--rule);
    }}
    .top-item {{
      min-height: 100%;
      border-right: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
      background: rgba(255, 255, 255, 0.58);
    }}
    .top-item a {{
      display: grid;
      align-content: start;
      min-height: 100%;
      padding: 14px;
    }}
    .top-item:hover {{
      background: var(--warm-hover);
    }}
    .top-media {{
      position: relative;
      aspect-ratio: 16 / 9;
      overflow: hidden;
      margin: -14px -14px 14px;
      border-bottom: 1px solid var(--rule);
      background:
        linear-gradient(135deg, rgba(155, 21, 63, 0.18), transparent 48%),
        linear-gradient(315deg, rgba(20, 92, 83, 0.24), transparent 48%),
        #242721;
    }}
    .top-media span {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: #f9f3e9;
      font-family: Newsreader, Georgia, serif;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: 0.08em;
      z-index: 0;
    }}
    .top-media img {{
      position: relative;
      z-index: 1;
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
      filter: saturate(0.9) contrast(1.03);
    }}
    .top-media.fallback span {{
      border-top: 1px solid rgba(255,255,255,0.35);
      border-bottom: 1px solid rgba(255,255,255,0.35);
      padding: 8px 0;
    }}
    .top-rule {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--accent);
      border-bottom: 1px solid var(--faint);
      padding-bottom: 10px;
      margin-bottom: 12px;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .top-item h3 {{
      margin: 0 0 10px;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 22px;
      line-height: 1.03;
      letter-spacing: 0;
    }}
    .top-item p {{
      margin: 0 0 14px;
      color: #303733;
    }}
    .top-item .rubric {{
      margin: 0 0 16px;
    }}
    .top-item .story-meta {{
      margin-top: auto;
      padding: 12px 0 0;
      border-top: 1px solid var(--faint);
    }}
    .lead-top {{
      grid-column: span 2;
      grid-row: span 2;
      background: #fffdf7;
    }}
    .lead-top a {{
      padding: 18px 22px 22px;
    }}
    .lead-top .top-media {{
      aspect-ratio: 21 / 9;
      margin: -18px -22px 18px;
    }}
    .lead-top h3 {{
      font-size: clamp(34px, 4vw, 56px);
      line-height: 0.96;
      max-width: 760px;
    }}
    .lead-top p {{
      max-width: 720px;
      font-size: 17px;
    }}
    .story {{
      background: var(--surface);
      border: 1px solid var(--rule);
      box-shadow: 0 10px 28px rgba(27, 22, 16, 0.05);
      overflow: hidden;
      transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
    }}
    .story-link {{
      display: grid;
      min-height: 100%;
    }}
    .story:hover {{
      border-color: var(--accent);
      box-shadow: 0 16px 38px rgba(27, 22, 16, 0.08);
    }}
    .story-kicker, .story-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 0 18px;
    }}
    .story-media {{
      position: relative;
      aspect-ratio: 16 / 9;
      overflow: hidden;
      background:
        linear-gradient(135deg, rgba(155, 21, 63, 0.18), transparent 44%),
        linear-gradient(315deg, rgba(20, 92, 83, 0.22), transparent 48%),
        #262720;
      border-bottom: 1px solid var(--rule);
    }}
    .story-media span {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: #f7f2e9;
      font-family: Newsreader, Georgia, serif;
      font-size: 28px;
      font-weight: 800;
      letter-spacing: 0.08em;
      z-index: 0;
    }}
    .story-media img {{
      position: relative;
      z-index: 1;
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }}
    .story-media.fallback {{
      display: grid;
      place-items: center;
      background:
        linear-gradient(135deg, rgba(155, 21, 63, 0.18), transparent 44%),
        linear-gradient(315deg, rgba(20, 92, 83, 0.22), transparent 48%),
        #262720;
      color: #f7f2e9;
    }}
    .story-media.fallback span {{
      position: static;
      font-family: Newsreader, Georgia, serif;
      font-size: 28px;
      font-weight: 800;
      letter-spacing: 0.08em;
      border-top: 1px solid rgba(255,255,255,0.38);
      border-bottom: 1px solid rgba(255,255,255,0.38);
      padding: 8px 0;
    }}
    .rank {{
      color: var(--accent);
      font-weight: 800;
    }}
    .story h3 {{
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 24px;
      font-weight: 800;
      line-height: 1.08;
      letter-spacing: 0;
      margin: 12px 18px 10px;
    }}
    .story p {{
      color: #313836;
      margin: 0 18px 18px;
    }}
    .rubric {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 0 18px 16px;
    }}
    .rubric span {{
      border: 1px solid rgba(155, 21, 63, 0.26);
      color: var(--accent);
      background: rgba(155, 21, 63, 0.055);
      padding: 4px 7px;
      font-size: 11px;
      font-weight: 650;
      line-height: 1;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .story-meta {{
      margin-top: auto;
      justify-content: space-between;
      border-top: 1px solid var(--faint);
      padding-top: 12px;
      padding-bottom: 16px;
    }}
    .high-impact {{
      border-color: rgba(155, 21, 63, 0.52);
    }}
    .material .story-meta span:first-child {{
      color: var(--accent-2);
    }}
    .lead {{
      border-top: 5px solid var(--accent);
    }}
    .lead h3 {{
      font-size: clamp(34px, 4vw, 56px);
      max-width: 760px;
    }}
    .lead .story-media {{
      aspect-ratio: 21 / 10;
    }}
    .compact {{
      box-shadow: none;
      background: var(--surface-2);
    }}
    .compact .story-link {{
      grid-template-columns: 112px minmax(0, 1fr);
      column-gap: 14px;
      padding: 0;
    }}
    .compact .story-media {{
      grid-column: 1;
      grid-row: 1 / span 4;
      aspect-ratio: auto;
      height: 100%;
      min-height: 138px;
      border-bottom: 0;
      border-right: 1px solid var(--rule);
    }}
    .compact .story-kicker,
    .compact h3,
    .compact p,
    .compact .rubric,
    .compact .story-meta {{
      grid-column: 2;
    }}
    .compact .story-media span {{
      font-size: 16px;
      writing-mode: vertical-rl;
    }}
    .compact h3 {{
      font-size: 18px;
      margin-right: 14px;
      margin-left: 0;
    }}
    .compact p {{
      margin-right: 14px;
      margin-left: 0;
    }}
    .compact .rubric {{
      margin-right: 14px;
      margin-left: 0;
    }}
    .compact .story-kicker, .compact .story-meta {{
      padding-left: 0;
      padding-right: 14px;
    }}
    .news-section {{
      margin-top: 42px;
      border-top: 2px solid var(--ink);
      padding-top: 18px;
    }}
    .section-head {{
      display: grid;
      grid-template-columns: minmax(220px, 0.4fr) minmax(0, 0.6fr);
      gap: 18px;
      align-items: end;
      margin-bottom: 14px;
    }}
    .section-head h2 {{
      margin: 0;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 30px;
      font-weight: 800;
      letter-spacing: 0;
    }}
    .section-head p {{
      margin: 0;
      color: var(--muted);
    }}
    .eyebrow {{
      color: var(--accent);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 4px !important;
    }}
    .story-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .watchlist {{
      margin-top: 38px;
      padding: 20px;
      background: #202523;
      color: #f4f6f2;
    }}
    .watchlist .section-head {{
      border-bottom: 1px solid rgba(255,255,255,0.22);
      padding-bottom: 14px;
    }}
    .watchlist .section-head p,
    .watchlist .story-kicker,
    .watchlist .story-meta {{
      color: #b7c0ba;
    }}
    .watchlist .story {{
      background: #2a302d;
      border-color: #46504b;
      box-shadow: none;
    }}
    .watchlist .story-media {{
      border-color: #46504b;
    }}
    .watchlist .story p {{
      color: #dce2de;
    }}
    .watchlist .rubric span {{
      border-color: rgba(255,255,255,0.24);
      background: rgba(255,255,255,0.08);
      color: #f2ded4;
    }}
    .watchlist .story-meta {{
      border-color: #46504b;
    }}
    .empty {{
      margin: 0;
      padding: 18px;
      border: 1px dashed var(--rule);
      color: var(--muted);
      background: var(--surface);
    }}
    .masthead {{
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      gap: 18px;
      padding: 0 0 16px;
      border-bottom: 1px solid var(--faint);
    }}
    .masthead > div:first-child {{
      grid-column: 2;
      text-align: center;
    }}
    .brand {{
      color: #e22c2f;
      font-size: clamp(60px, 8.4vw, 112px);
      font-style: italic;
      line-height: 0.76;
      margin: 0;
    }}
    .dateline {{
      max-width: none;
      margin-top: 10px;
      font-size: 12px;
    }}
    .issue-box {{
      justify-self: end;
      min-width: 132px;
      padding: 10px 12px;
      border: 0;
      background: transparent;
      text-align: right;
    }}
    .issue-box strong {{
      color: var(--ink);
      font-family: Inter, "Segoe UI", system-ui, sans-serif;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .issue-box span {{
      display: inline-block;
      margin-top: 8px;
      padding: 7px 10px;
      background: #e22c2f;
      color: #fff;
      font-size: 10px;
      font-weight: 800;
    }}
    .nav {{
      justify-content: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--faint);
    }}
    .nav a {{
      border: 0;
      background: transparent;
      padding: 6px 12px;
      color: #191b18;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: none;
    }}
    .nav a:hover {{
      background: transparent;
      color: #e22c2f;
    }}
    .editorial-strip {{
      margin-top: 14px;
      border-color: var(--faint);
      border-top: 1px solid var(--faint);
      background: #fff;
    }}
    .front-page {{
      margin: 26px 0 22px;
      padding: 0;
      border: 0;
      background: transparent;
    }}
    .magazine-layout {{
      display: grid;
      grid-template-columns: minmax(190px, 0.74fr) minmax(360px, 1.55fr) minmax(220px, 0.9fr);
      gap: 22px;
      align-items: start;
    }}
    .rail-head {{
      margin-bottom: 14px;
    }}
    .rail-head h2 {{
      margin: 0;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 34px;
      font-weight: 800;
      line-height: 0.95;
      letter-spacing: 0;
    }}
    .rail-head p {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      line-height: 1.25;
      text-transform: uppercase;
    }}
    .rail-item {{
      display: block;
      padding: 11px 0 12px;
      border-bottom: 1px solid var(--faint);
    }}
    .rail-item span,
    .popular-item span,
    .breaking-copy span {{
      display: inline-block;
      color: #e22c2f;
      font-size: 10px;
      font-weight: 900;
      line-height: 1;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .rail-item strong {{
      display: block;
      margin-top: 6px;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 17px;
      font-weight: 800;
      line-height: 1.05;
    }}
    .rail-item small,
    .popular-item small,
    .breaking-copy small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .hero-story {{
      min-width: 0;
      background: #111;
    }}
    .hero-story a {{
      position: relative;
      display: block;
      min-height: 540px;
      overflow: hidden;
      color: #fff;
    }}
    .hero-media {{
      position: absolute;
      inset: 0;
      background:
        linear-gradient(135deg, rgba(226, 44, 47, 0.42), transparent 38%),
        linear-gradient(180deg, transparent 36%, rgba(0, 0, 0, 0.88)),
        #252525;
    }}
    .hero-media img {{
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
      opacity: 0.9;
    }}
    .hero-media span {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: rgba(255,255,255,0.24);
      font-family: Newsreader, Georgia, serif;
      font-size: 76px;
      font-weight: 800;
      z-index: 0;
    }}
    .hero-copy {{
      position: absolute;
      inset: auto 0 0;
      padding: 28px 28px 30px;
      background: linear-gradient(180deg, transparent, rgba(0,0,0,0.68) 18%, rgba(0,0,0,0.92));
    }}
    .hero-kicker {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 10px;
    }}
    .hero-kicker span {{
      background: #e22c2f;
      padding: 5px 7px;
      font-size: 10px;
      font-weight: 900;
      line-height: 1;
      text-transform: uppercase;
    }}
    .hero-kicker small {{
      color: rgba(255,255,255,0.74);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .hero-copy h2 {{
      max-width: 720px;
      margin: 0;
      font-family: Inter, "Segoe UI", system-ui, sans-serif;
      font-size: clamp(34px, 4.8vw, 58px);
      font-weight: 900;
      line-height: 0.94;
      letter-spacing: 0;
    }}
    .hero-copy p {{
      max-width: 610px;
      margin: 12px 0 0;
      color: rgba(255,255,255,0.9);
      font-size: 15px;
      line-height: 1.42;
    }}
    .popular-rail {{
      background: #fff;
      padding: 18px 18px 8px;
      box-shadow: 0 10px 30px rgba(15, 15, 15, 0.08);
    }}
    .compact-head h2 {{
      font-size: 36px;
    }}
    .popular-item {{
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr);
      gap: 12px;
      padding: 12px 0;
      border-top: 1px solid var(--faint);
    }}
    .popular-thumb {{
      position: relative;
      aspect-ratio: 4 / 3;
      overflow: hidden;
      background: #1f2522;
    }}
    .popular-thumb img {{
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }}
    .popular-thumb span {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: rgba(255,255,255,0.72);
      font-size: 10px;
      font-weight: 900;
      z-index: 0;
    }}
    .popular-item strong {{
      display: block;
      margin-top: 5px;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 17px;
      font-weight: 800;
      line-height: 1.03;
    }}
    .breaking-gallery {{
      margin: 28px 0 30px;
      padding: 22px;
      border-top: 1px solid var(--faint);
      border-bottom: 1px solid var(--faint);
      background: #fff;
    }}
    .module-head {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .module-head span {{
      display: inline-block;
      background: #e22c2f;
      color: #fff;
      padding: 8px 12px;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 20px;
      font-style: italic;
      font-weight: 800;
      line-height: 1;
    }}
    .module-head strong {{
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 20px;
      line-height: 1.1;
      text-align: right;
    }}
    .breaking-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 16px;
    }}
    .breaking-card {{
      background: #111;
      min-width: 0;
    }}
    .breaking-card a {{
      display: grid;
      min-height: 100%;
      color: #fff;
    }}
    .breaking-media {{
      position: relative;
      aspect-ratio: 4 / 3;
      overflow: hidden;
      background: #202523;
    }}
    .breaking-media img {{
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
      opacity: 0.9;
    }}
    .breaking-media span {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: rgba(255,255,255,0.64);
      font-weight: 900;
      z-index: 0;
    }}
    .breaking-copy {{
      padding: 12px 12px 14px;
    }}
    .breaking-copy span {{
      background: #e22c2f;
      color: #fff;
      padding: 4px 6px;
    }}
    .breaking-copy strong {{
      display: block;
      margin-top: 8px;
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 19px;
      font-weight: 800;
      line-height: 1.02;
    }}
    .masthead::before {{
      content: none;
    }}
    .issue-box {{
      border-left: 1px solid var(--faint);
      color: var(--ink);
    }}
    .issue-box strong {{
      color: var(--ink);
      font-family: Newsreader, Georgia, "Times New Roman", serif;
      font-size: 34px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: none;
    }}
    .issue-box span {{
      margin-top: 6px;
      padding: 0;
      background: transparent;
      color: var(--muted);
      font-size: 10px;
      font-weight: 900;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .news-section {{
      margin-top: 34px;
      padding: 22px;
      border: 1px solid var(--faint);
      border-top: 3px solid var(--ink);
      background: #fff;
    }}
    .news-section + .news-section {{
      margin-top: 24px;
    }}
    .news-section .section-head {{
      grid-template-columns: minmax(240px, 0.42fr) minmax(0, 0.58fr);
      align-items: end;
      padding-bottom: 15px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--faint);
    }}
    .news-section .section-head h2 {{
      font-size: clamp(32px, 3.6vw, 48px);
      line-height: 0.95;
    }}
    .news-section .section-head > p {{
      max-width: 640px;
      justify-self: end;
      color: #4b514d;
      font-size: 13px;
      line-height: 1.45;
      text-align: right;
    }}
    .news-section .eyebrow {{
      display: inline-block;
      margin-bottom: 9px !important;
      padding: 6px 8px;
      background: #e22c2f;
      color: #fff;
      font-size: 10px;
      line-height: 1;
      letter-spacing: 0.06em;
    }}
    .news-section .story-grid {{
      gap: 16px;
    }}
    .news-section .story {{
      border-color: var(--faint);
      background: #fff;
      box-shadow: none;
    }}
    .news-section .story:hover {{
      border-color: #e22c2f;
      box-shadow: 0 12px 30px rgba(18, 20, 17, 0.07);
      transform: translateY(-1px);
    }}
    .news-section .story h3 {{
      font-size: 23px;
    }}
    .news-section .story-media {{
      border-bottom-color: var(--faint);
      background:
        linear-gradient(135deg, rgba(226, 44, 47, 0.22), transparent 42%),
        linear-gradient(315deg, rgba(18, 20, 17, 0.24), transparent 48%),
        #242521;
    }}
    .news-section .rubric span {{
      border-color: rgba(226, 44, 47, 0.28);
      background: rgba(226, 44, 47, 0.065);
      color: #e22c2f;
    }}
    .section-local {{
      border-top-color: #e22c2f;
    }}
    .watchlist {{
      color: var(--ink);
    }}
    .watchlist .section-head {{
      border-bottom-color: var(--faint);
    }}
    .watchlist .section-head p,
    .watchlist .story-kicker,
    .watchlist .story-meta {{
      color: var(--muted);
    }}
    .watchlist .story {{
      background: #fff;
      border-color: var(--faint);
      box-shadow: none;
    }}
    .watchlist .story-media {{
      border-color: var(--faint);
    }}
    .watchlist .story p {{
      color: #313836;
    }}
    .watchlist .rubric span {{
      border-color: rgba(226, 44, 47, 0.28);
      background: rgba(226, 44, 47, 0.065);
      color: #e22c2f;
    }}
    .watchlist .story-meta {{
      border-color: var(--faint);
    }}
    :root[data-theme="dark"] .brand {{
      color: var(--brand-red);
      text-shadow: 0 0 22px rgba(255, 53, 56, 0.12);
    }}
    :root[data-theme="dark"] .nav a {{
      color: var(--ink);
    }}
    :root[data-theme="dark"] .nav a:hover {{
      color: var(--brand-red);
    }}
    :root[data-theme="dark"] .editorial-strip,
    :root[data-theme="dark"] .popular-rail,
    :root[data-theme="dark"] .breaking-gallery,
    :root[data-theme="dark"] .news-section,
    :root[data-theme="dark"] .story,
    :root[data-theme="dark"] .news-section .story,
    :root[data-theme="dark"] .watchlist .story,
    :root[data-theme="dark"] .empty {{
      background: var(--surface);
      color: var(--ink);
    }}
    :root[data-theme="dark"] .top-item {{
      background: rgba(24, 26, 23, 0.74);
    }}
    :root[data-theme="dark"] .popular-rail,
    :root[data-theme="dark"] .story {{
      box-shadow: var(--shadow);
    }}
    :root[data-theme="dark"] .story p,
    :root[data-theme="dark"] .watchlist .story p,
    :root[data-theme="dark"] .top-item p {{
      color: var(--body-copy);
    }}
    :root[data-theme="dark"] .news-section .section-head > p {{
      color: var(--section-copy);
    }}
    :root[data-theme="dark"] .rubric span,
    :root[data-theme="dark"] .news-section .rubric span,
    :root[data-theme="dark"] .watchlist .rubric span {{
      border-color: rgba(255, 53, 56, 0.36);
      background: rgba(255, 53, 56, 0.1);
      color: #ff6b6d;
    }}
    :root[data-theme="dark"] .story-media,
    :root[data-theme="dark"] .news-section .story-media {{
      background:
        linear-gradient(135deg, rgba(255, 53, 56, 0.22), transparent 42%),
        linear-gradient(315deg, rgba(255, 255, 255, 0.1), transparent 48%),
        #1a1c19;
    }}
    :root[data-theme="dark"] .popular-thumb,
    :root[data-theme="dark"] .breaking-media {{
      background: #171915;
    }}
    :root[data-theme="dark"] .hero-media {{
      background:
        linear-gradient(135deg, rgba(255, 53, 56, 0.36), transparent 38%),
        linear-gradient(180deg, transparent 34%, rgba(0, 0, 0, 0.9)),
        #171915;
    }}
    :root[data-theme="dark"] .breaking-card {{
      background: #121310;
      border: 1px solid var(--faint);
    }}
    :root[data-theme="dark"] .module-head span,
    :root[data-theme="dark"] .hero-kicker span,
    :root[data-theme="dark"] .breaking-copy span,
    :root[data-theme="dark"] .news-section .eyebrow {{
      background: var(--brand-red);
      color: #fff;
    }}
    :root[data-theme="dark"] .section-local {{
      border-top-color: var(--brand-red);
    }}
    @media (max-width: 820px) {{
      .page {{ padding: 20px 14px 44px; }}
      .page {{
        width: 100%;
        max-width: 100vw;
        overflow: hidden;
      }}
      body {{
        overflow-x: hidden;
      }}
      .masthead, .section-head, .story-grid {{
        grid-template-columns: 1fr;
      }}
      .masthead {{
        text-align: center;
      }}
      .masthead::before {{
        justify-self: center;
      }}
      .masthead > div:first-child {{
        grid-column: auto;
        min-width: 0;
      }}
      .issue-box {{
        justify-self: center;
        text-align: center;
      }}
      .mast-actions {{
        justify-self: center;
      }}
      .theme-toggle {{
        justify-self: center;
      }}
      .magazine-layout,
      .breaking-grid {{
        grid-template-columns: 1fr;
      }}
      .magazine-layout {{
        display: block;
      }}
      .fresh-rail,
      .popular-rail,
      .hero-story {{
        width: calc(100vw - 28px);
      }}
      .brand {{
        max-width: 100%;
        font-size: 44px;
        line-height: 0.82;
        white-space: nowrap;
      }}
      .dateline {{
        max-width: 340px;
        margin-left: auto;
        margin-right: auto;
      }}
      .nav {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0;
        justify-content: stretch;
        overflow: hidden;
      }}
      .nav a {{
        padding: 7px 7px;
        font-size: 10px;
        text-align: center;
      }}
      .editorial-strip {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .editorial-strip div {{
        min-width: 0;
      }}
      .rail-head p,
      .rail-item strong,
      .popular-item strong,
      .breaking-copy strong,
      .hero-copy h2 {{
        overflow-wrap: anywhere;
        word-break: break-word;
      }}
      .magazine-layout,
      .fresh-rail,
      .popular-rail,
      .hero-story,
      .rail-item,
      .popular-item,
      .breaking-card {{
        min-width: 0;
        max-width: 100%;
      }}
      .rail-head p {{
        max-width: 100%;
        font-size: 11px;
      }}
      .hero-story a {{
        min-height: 500px;
      }}
      .popular-rail {{
        padding: 16px;
      }}
      .breaking-gallery {{
        padding: 16px;
      }}
      .module-head {{
        display: block;
      }}
      .module-head strong {{
        display: block;
        margin-top: 12px;
        text-align: left;
      }}
      .news-section {{
        padding: 16px;
        margin-top: 26px;
      }}
      .news-section .section-head {{
        grid-template-columns: 1fr;
        gap: 10px;
      }}
      .news-section .section-head > p {{
        justify-self: start;
        text-align: left;
      }}
      .news-section .story-grid {{
        grid-template-columns: 1fr;
      }}
      .front-head {{
        display: block;
      }}
      .front-head h2 {{
        text-align: left;
        margin-top: 20px;
      }}
      .top-grid {{
        grid-template-columns: 1fr;
        border-left: 0;
      }}
      .top-item {{
        border-left: 1px solid var(--rule);
      }}
      .lead-top {{
        grid-column: auto;
        grid-row: auto;
      }}
      .lead-top h3 {{
        font-size: 34px;
      }}
      .lead-top .top-media {{
        aspect-ratio: 16 / 9;
      }}
      .editorial-strip {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .editorial-strip div {{
        border-bottom: 1px solid var(--rule);
      }}
      .editorial-strip div:nth-child(2n) {{
        border-right: 0;
      }}
      .brand {{ font-size: 44px; }}
      .issue-box {{
        border-left: 0;
        text-align: center;
      }}
      .lead h3 {{ font-size: 28px; }}
      .story h3 {{ font-size: 21px; }}
      .compact .story-link {{
        grid-template-columns: 96px minmax(0, 1fr);
      }}
      .compact .story-media {{
        min-height: 150px;
      }}
      .nav {{ position: static; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="masthead">
      <div>
        <h1 class="brand">News Brief</h1>
        <p class="dateline">Generated {generated_at.strftime('%A, %d %B %Y, %H:%M IST')}. A compact scan of global, India, technology, and coastal Karnataka signals.</p>
      </div>
      <div class="mast-actions">
        <button class="theme-toggle" type="button" aria-label="Switch to dark mode" aria-pressed="false">
          <span class="theme-icon" aria-hidden="true"></span>
          <span class="theme-text">Dark</span>
        </button>
        <div class="issue-box">
          <strong>{total_count}</strong>
          <span>editor selected</span>
        </div>
      </div>
    </header>
    <nav class="nav" aria-label="Brief sections">
      <a href="#global">Global</a>
      <a href="#india">India</a>
      <a href="#tech">Tech / AI</a>
      <a href="#local">Mangalore / Coast</a>
      <a href="#breaking">Breaking</a>
      <a href="#watchlist">Watchlist</a>
    </nav>
    <section class="editorial-strip" aria-label="Editorial summary">
      <div>
        <span>Editorial threshold</span>
        <strong>{html.escape(strongest_label)}</strong>
      </div>
      {section_stats}
    </section>
    <section class="front-page" aria-label="Top stories">
      <div class="magazine-layout">
        <aside class="fresh-rail" aria-label="Fresh signals">
          <div class="rail-head">
            <h2>Fresh Signals</h2>
            <p>Editor-picked updates worth knowing before the day moves.</p>
          </div>
          {fresh_rail or '<p class="empty">No additional fresh signals crossed the threshold.</p>'}
        </aside>
        {hero_html}
        <aside class="popular-rail" aria-label="Popular">
          <div class="rail-head compact-head">
            <h2>Popular</h2>
          </div>
          {popular_rail or '<p class="empty">No popular items crossed the threshold.</p>'}
        </aside>
      </div>
    </section>
    {breaking_html}
    {''.join(section_blocks)}
    <section id="watchlist" class="news-section watchlist">
      <div class="section-head">
        <div>
          <p class="eyebrow">WATCHLIST</p>
          <h2>Watchlist</h2>
        </div>
        <p>{html.escape(section_deck("watchlist"))}</p>
      </div>
      <div class="story-grid">{watchlist_cards}</div>
    </section>
  </main>
  <script>
    (() => {{
      const root = document.documentElement;
      const toggle = document.querySelector(".theme-toggle");
      const label = toggle?.querySelector(".theme-text");
      if (!toggle || !label) return;

      const applyTheme = (theme) => {{
        const isDark = theme === "dark";
        root.dataset.theme = theme;
        toggle.setAttribute("aria-pressed", String(isDark));
        toggle.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
        label.textContent = isDark ? "Light" : "Dark";
      }};

      applyTheme(root.dataset.theme === "dark" ? "dark" : "light");
      toggle.addEventListener("click", () => {{
        const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
        localStorage.setItem("newsbrief-theme", nextTheme);
        applyTheme(nextTheme);
      }});
    }})();
  </script>
</body>
</html>
"""


def write_outputs(
    markdown: str,
    sections: dict[str, list[Story]],
    settings: dict[str, Any],
    generated_at: datetime,
) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive_dir = OUTPUT_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stem = generated_at.strftime("%Y-%m-%d-morning-brief")
    md_path = archive_dir / f"{stem}.md"
    html_path = archive_dir / f"{stem}.html"
    index_path = OUTPUT_DIR / "index.html"
    latest_md_path = OUTPUT_DIR / "latest.md"
    html_text = render_html_brief(sections, settings, generated_at)
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    latest_md_path.write_text(markdown, encoding="utf-8")
    index_path.write_text(html_text, encoding="utf-8")
    return latest_md_path, index_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the daily local news brief.")
    parser.add_argument("--hours", type=int, default=None, help="Lookback window in hours. Defaults to settings.json.")
    parser.add_argument("--open", action="store_true", help="Open the generated HTML brief in the default browser.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_json(CONFIG_DIR / "settings.json")
    sources = load_json(CONFIG_DIR / "sources.json")
    hours = args.hours or int(settings["default_hours"])

    stories = collect(settings, sources, hours)
    sections = select_sections(stories, settings)
    tz = ZoneInfo(settings["timezone"])
    generated_at = datetime.now(tz)
    markdown = render_markdown(sections, settings, generated_at)
    md_path, html_path = write_outputs(markdown, sections, settings, generated_at)

    print(f"Collected {len(stories)} unique recent stories.")
    print(f"Markdown: {md_path}")
    print(f"HTML: {html_path}")

    if args.open:
        import webbrowser

        webbrowser.open(html_path.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
