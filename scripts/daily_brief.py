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

    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc).astimezone(fallback_tz)
        except ValueError:
            continue
    return None


def text_from_child(node: ElementTree.Element, child_name: str) -> str:
    child = node.find(child_name)
    if child is not None and child.text:
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
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    stopwords = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "with", "as", "by"}
    return " ".join(word for word in title.split() if word not in stopwords)


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if " " in keyword:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


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
    value = clean_text(value)
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
    score = quality * 2
    reasons: list[str] = [f"source {quality}/5"]

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
        summary = (
            text_from_child(item, "description")
            or text_from_child(item, "summary")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}summary")
        )
        published = parse_datetime(
            text_from_child(item, "pubDate")
            or text_from_child(item, "published")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}published")
            or text_from_child(item, "updated")
            or text_from_child(item, "{http://www.w3.org/2005/Atom}updated"),
            tz,
        )
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, summary, settings):
            continue
        if not has_bucket_relevance(title, summary, source["bucket"]):
            continue
        image_url = image_from_rss_item(item, summary)
        score, reason = score_item(title, summary, source["bucket"], int(source["quality"]), settings, now, published)
        stories.append(
            Story(
                title=title,
                link=link,
                source=source["name"],
                bucket=source["bucket"],
                published=published,
                summary=summary,
                image_url=image_url,
                quality=int(source["quality"]),
                score=score,
                reason=reason,
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
        link = clean_text(match.group("link"))
        published = parse_daijiworld_datetime(match.group("date"), tz)
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, "", settings):
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
        link = clean_text(article.get("url", ""))
        summary = clean_text(article.get("seendate", ""))
        published = parse_datetime(article.get("seendate"), tz)
        if not title or not link or published is None or published < since or published > now + timedelta(hours=1):
            continue
        if is_noise(title, summary, settings):
            continue
        if not has_bucket_relevance(title, summary, source["bucket"]):
            continue
        domain = article.get("domain") or urllib.parse.urlsplit(link).netloc
        source_name = f"{source['name']} / {domain}"
        score, reason = score_item(title, summary, source["bucket"], int(source["quality"]), settings, now, published)
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
    return sorted(unique, key=lambda item: (item.score, item.published), reverse=True)


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
    sections: dict[str, list[Story]] = {}
    for bucket in ("global", "india", "tech", "local"):
        bucket_stories = [story for story in stories if story.bucket == bucket]
        sections[bucket] = bucket_stories[: int(max_items[bucket])]

    seen_links = {canonical_link(story.link) for bucket in sections.values() for story in bucket}
    top_pool = [story for story in stories if canonical_link(story.link) not in seen_links or story.score >= 20]
    sections["top"] = top_pool[: int(max_items["top"])]

    selected = {canonical_link(story.link) for bucket in sections.values() for story in bucket}
    sections["watchlist"] = [
        story for story in stories if canonical_link(story.link) not in selected and story.score >= 12
    ][: int(max_items["watchlist"])]
    return sections


def story_line(story: Story, tz: ZoneInfo) -> str:
    summary = sentence(story.summary) or "Open the source for the full update."
    timestamp = story.published.astimezone(tz).strftime("%d %b, %H:%M IST")
    return (
        f"**{headline(story.title)}**\n"
        f"{summary} "
        f"[Source: {story.source}]({story.link}) "
        f"`{timestamp}; score {story.score}`"
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
        "watchlist": "Lower-confidence or secondary items worth a quick scan, not a main briefing slot.",
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
    class_name = "story compact" if compact else "story"
    class_name += " lead" if lead else ""
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
          <div class="story-meta"><span>Score {story.score}</span><span>Open source</span></div>
        </a>
      </article>
    """


def render_html_brief(sections: dict[str, list[Story]], settings: dict[str, Any], generated_at: datetime) -> str:
    tz = generated_at.tzinfo or ZoneInfo(settings["timezone"])
    labels = settings["section_labels"]
    total_count = sum(len(items) for items in sections.values())
    lead = sections["top"][0] if sections["top"] else None
    top_rest = sections["top"][1:]

    top_cards = "\n".join(
        html_story_card(story, tz, index, compact=True)
        for index, story in enumerate(top_rest, start=2)
    )
    section_blocks: list[str] = []
    for bucket in ("global", "india", "tech", "local"):
        stories = sections[bucket]
        cards = "\n".join(html_story_card(story, tz) for story in stories)
        if not cards:
            cards = '<p class="empty">No strong, recent items found for this section.</p>'
        section_blocks.append(
            f"""
            <section id="{bucket}" class="news-section">
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

    watchlist_cards = "\n".join(html_story_card(story, tz, compact=True) for story in sections["watchlist"])
    if not watchlist_cards:
        watchlist_cards = '<p class="empty">No additional watchlist items crossed the threshold.</p>'

    lead_html = (
        html_story_card(lead, tz, 1, lead=True)
        if lead
        else '<p class="empty">No high-confidence top story found in the configured window.</p>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings["brief_title"])}</title>
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
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.55 Inter, "Segoe UI", system-ui, sans-serif;
    }}
    a {{ color: inherit; text-decoration: none; }}
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
      background: rgba(246, 243, 238, 0.94);
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
    .lead-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(280px, 0.8fr);
      gap: 18px;
      align-items: start;
      margin: 24px 0 30px;
    }}
    .top-stack {{
      display: grid;
      gap: 12px;
    }}
    .story {{
      background: var(--surface);
      border: 1px solid var(--rule);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .story-link {{
      display: grid;
      min-height: 100%;
    }}
    .story:hover {{
      border-color: var(--accent);
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
    .story-meta {{
      margin-top: auto;
      justify-content: space-between;
      border-top: 1px solid var(--faint);
      padding-top: 12px;
      padding-bottom: 16px;
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
    .compact .story-kicker, .compact .story-meta {{
      padding-left: 0;
      padding-right: 14px;
    }}
    .news-section {{
      margin-top: 36px;
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
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
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
    .watchlist .story p {{
      color: #dce2de;
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
    @media (max-width: 820px) {{
      .page {{ padding: 20px 14px 44px; }}
      .masthead, .lead-layout, .section-head, .story-grid {{
        grid-template-columns: 1fr;
      }}
      .brand {{ font-size: 64px; }}
      .issue-box {{ text-align: left; }}
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
      <div class="issue-box">
        <strong>{total_count}</strong>
        <span>selected items</span>
      </div>
    </header>
    <nav class="nav" aria-label="Brief sections">
      <a href="#global">Global</a>
      <a href="#india">India</a>
      <a href="#tech">Tech / AI</a>
      <a href="#local">Coastal</a>
      <a href="#watchlist">Watchlist</a>
    </nav>
    <section class="lead-layout" aria-label="Top stories">
      {lead_html}
      <div class="top-stack">{top_cards}</div>
    </section>
    {''.join(section_blocks)}
    <section id="watchlist" class="watchlist">
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
