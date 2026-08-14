from __future__ import annotations

import argparse
import functools
import hashlib
import html
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
import yaml
from dateutil import parser as dtparser


UA = "EPS-GovCon-Morning-Brief/1.0 (+internal business intelligence)"
FR_API = "https://www.federalregister.gov/api/v1/documents.json"
SAM_API = "https://api.sam.gov/opportunities/v2/search"
USASPENDING_API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    score: int = 0
    agencies: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def age_hours(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.published).total_seconds() / 3600)

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


def utc(dt) -> datetime:
    if not dt:
        return datetime.now(timezone.utc)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        parsed = dtparser.parse(str(dt))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


@functools.lru_cache(maxsize=8192)
def _kw_pattern(kw_lower: str) -> re.Pattern:
    # Match a keyword only as a whole token, so acronyms like "SOF", "AI" or
    # "PEO" don't match inside "software", "training" or "people".
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw_lower) + r"(?![a-z0-9])")


def keyword_in(text_lower: str, keyword: str) -> bool:
    kw = str(keyword).lower().strip()
    if not kw:
        return False
    return _kw_pattern(kw).search(text_lower) is not None


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def google_news_items(config: dict) -> list[Item]:
    items: list[Item] = []
    for nq in config.get("news_queries", []):
        q = quote_plus(nq["query"])
        feed_url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(feed_url, agent=UA)
        for e in feed.entries:
            title = clean_text(e.get("title", ""))
            link = e.get("link", "")
            summary = clean_text(e.get("summary", ""))
            pub = utc(e.get("published") or e.get("updated"))
            source = nq["name"]
            if e.get("source"):
                try:
                    source = e.source.get("title") or source
                except Exception:
                    pass
            if title and link:
                items.append(Item(title=title, url=link, source=source, published=pub, summary=summary))
    return items


def federal_register_items(config: dict) -> list[Item]:
    fr = config.get("federal_register", {})
    if not fr.get("enabled", True):
        return []

    start = (datetime.now(timezone.utc) - timedelta(hours=config["brief"]["lookback_hours"])).date().isoformat()
    items: list[Item] = []
    terms = fr.get("terms", [])

    for term in terms:
        params = {
            "per_page": 100,
            "order": "newest",
            "conditions[publication_date][gte]": start,
            "conditions[term]": term,
        }
        try:
            r = requests.get(FR_API, params=params, headers={"User-Agent": UA}, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"[warn] Federal Register query failed for {term!r}: {exc}", file=sys.stderr)
            continue

        for doc in data.get("results", []):
            agencies = ", ".join(a.get("name", "") for a in doc.get("agencies", []) if a.get("name"))
            title = clean_text(doc.get("title", ""))
            url = doc.get("html_url") or doc.get("pdf_url") or ""
            summary = clean_text(doc.get("abstract", "") or agencies)
            published = utc(doc.get("publication_date"))
            if title and url:
                items.append(Item(
                    title=title,
                    url=url,
                    source=f"Federal Register{': ' + agencies if agencies else ''}",
                    published=published,
                    summary=summary,
                ))
    return items


def sam_items(config: dict) -> list[Item]:
    scfg = config.get("sam", {})
    if not scfg.get("enabled", False):
        return []
    api_key = os.getenv("SAM_API_KEY", "").strip()
    if not api_key:
        print("[warn] SAM collector enabled but SAM_API_KEY is not set.", file=sys.stderr)
        return []

    now = datetime.now(timezone.utc)
    days = int(scfg.get("posted_days", 2))
    params = {
        "api_key": api_key,
        "postedFrom": (now - timedelta(days=days)).strftime("%m/%d/%Y"),
        "postedTo": now.strftime("%m/%d/%Y"),
        "limit": int(scfg.get("limit", 100)),
        "offset": 0,
    }
    try:
        r = requests.get(SAM_API, params=params, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[warn] SAM API failed: {exc}", file=sys.stderr)
        return []

    items: list[Item] = []
    for o in data.get("opportunitiesData", []):
        title = clean_text(o.get("title", ""))
        notice_id = o.get("noticeId", "")
        url = f"https://sam.gov/opp/{notice_id}/view" if notice_id else "https://sam.gov/opportunities"
        desc = " | ".join(filter(None, [
            o.get("department"),
            o.get("subTier"),
            o.get("office"),
            o.get("type"),
            o.get("solicitationNumber"),
        ]))
        pub = utc(o.get("postedDate"))
        if title:
            items.append(Item(title=title, url=url, source="SAM.gov", published=pub, summary=desc))
    return items


def gao_protest_items(config: dict) -> list[Item]:
    """Pull GAO bid-protest decisions directly from GAO RSS feeds.

    GAO publishes protest decisions as RSS. Feed URLs are configurable so the
    endpoint can be corrected without code changes. Items are pre-tagged as
    Protest events; scoring still applies agency/capability/company weights and
    the gao.gov source boost.
    """
    gcfg = config.get("gao_protests", {})
    if not gcfg.get("enabled", False):
        return []

    items: list[Item] = []
    for feed_url in gcfg.get("feeds", []):
        try:
            feed = feedparser.parse(feed_url, agent=UA)
        except Exception as exc:
            print(f"[warn] GAO feed failed for {feed_url!r}: {exc}", file=sys.stderr)
            continue
        if getattr(feed, "bozo", 0) and not feed.entries:
            print(f"[warn] GAO feed returned no entries: {feed_url}", file=sys.stderr)
            continue
        for e in feed.entries:
            title = clean_text(e.get("title", ""))
            link = e.get("link", "")
            summary = clean_text(e.get("summary", "") or e.get("description", ""))
            pub = utc(e.get("published") or e.get("updated"))
            if title and link:
                item = Item(
                    title=title,
                    url=link,
                    source="GAO Bid Protests",
                    published=pub,
                    summary=summary,
                )
                item.events.append("Protest")
                item.reasons.append("GAO bid-protest docket")
                items.append(item)
    return items


def usaspending_items(config: dict) -> list[Item]:
    """Pull recent, high-value prime contract awards from the public
    USAspending.gov API (FPDS data) for the configured awarding agencies.

    This is the quantitative backbone of the market view: who is winning, how
    much, and for what. No API key required. Items are sourced from
    usaspending.gov (a .gov source, so they clear the relevance gate) and
    phrased so they tag as Award events.
    """
    ucfg = config.get("usaspending", {})
    if not ucfg.get("enabled", False):
        return []

    now = datetime.now(timezone.utc)
    days = int(ucfg.get("lookback_days", 30))
    start = (now - timedelta(days=days)).date().isoformat()
    end = now.date().isoformat()
    min_amount = float(ucfg.get("min_amount", 0) or 0)
    limit = int(ucfg.get("limit", 25))
    agencies = ucfg.get("agencies", []) or []

    fields = [
        "Award ID", "Recipient Name", "Award Amount", "Description",
        "Awarding Agency", "Awarding Sub Agency", "Start Date", "Award Type",
    ]

    items: list[Item] = []
    # One query per agency keeps each result set focused and within API limits.
    queries = [{"name": a} for a in agencies] or [{"name": None}]
    for q in queries:
        filters: dict = {
            "time_period": [{"start_date": start, "end_date": end}],
            "award_type_codes": ["A", "B", "C", "D"],  # definitive contracts
        }
        if q["name"]:
            filters["agencies"] = [
                {"type": "awarding", "tier": "toptier", "name": q["name"]}
            ]
        payload = {
            "filters": filters,
            "fields": fields,
            "sort": "Award Amount",
            "order": "desc",
            "limit": limit,
            "page": 1,
        }
        try:
            r = requests.post(USASPENDING_API, json=payload,
                              headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            label = q["name"] or "all agencies"
            print(f"[warn] USAspending query failed for {label!r}: {exc}", file=sys.stderr)
            continue

        for o in data.get("results", []):
            try:
                amount = float(o.get("Award Amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if amount < min_amount:
                continue
            recipient = clean_text(o.get("Recipient Name") or "Unknown recipient")
            agency = clean_text(o.get("Awarding Agency") or (q["name"] or ""))
            sub = clean_text(o.get("Awarding Sub Agency") or "")
            gid = o.get("generated_internal_id") or ""
            url = f"https://www.usaspending.gov/award/{gid}/" if gid else "https://www.usaspending.gov"
            # Phrase so it reads as, and tags as, an award.
            amt = f"${amount:,.0f}" if amount else "amount N/A"
            title = f"{recipient}: {amt} contract award — {agency}"
            desc = clean_text(o.get("Description") or "")
            summary = " | ".join(filter(None, [agency, sub, desc]))
            pub = utc(o.get("Start Date"))
            items.append(Item(title=title, url=url, source="USAspending.gov",
                              published=pub, summary=summary))
    return items


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\s+-\s+[^-]{2,50}$", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def dedupe(items: Iterable[Item]) -> list[Item]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[Item] = []
    for item in sorted(items, key=lambda x: x.published, reverse=True):
        nt = norm_title(item.title)
        url_key = re.sub(r"[?&](utm_[^=&]+|gclid)=[^&]+", "", item.url)
        if url_key in seen_urls or nt in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(nt)
        out.append(item)
    return out


def score_items(items: list[Item], config: dict) -> list[Item]:
    priority = config.get("source_priority", {})
    lookback = float(config["brief"]["lookback_hours"])

    def apply_group(item: Item, group_name: str, group_cfg: dict, bucket: list[str]) -> int:
        text = f"{item.title} {item.summary}".lower()
        matches = []
        for kw in group_cfg.get("keywords", []):
            if keyword_in(text, kw):
                matches.append(str(kw))
        if matches:
            if group_name not in bucket:
                bucket.append(group_name)
            item.reasons.append(f"{group_name}: {', '.join(matches[:3])}")
            return int(group_cfg.get("weight", 1))
        return 0

    for item in items:
        score = 0
        for name, cfg in config.get("agency_groups", {}).items():
            score += apply_group(item, name, cfg, item.agencies)
        for name, cfg in config.get("company_groups", {}).items():
            score += apply_group(item, name, cfg, item.companies)
        for name, cfg in config.get("capability_groups", {}).items():
            score += apply_group(item, name, cfg, item.capabilities)
        for name, cfg in config.get("event_groups", {}).items():
            score += apply_group(item, name, cfg, item.events)

        for domain, boost in priority.items():
            if item.domain.endswith(domain):
                score += int(boost)
                item.reasons.append(f"trusted source: {domain}")
                break

        # Recency boost
        if item.age_hours <= 8:
            score += 3
            item.reasons.append("published within 8h")
        elif item.age_hours <= 24:
            score += 2
        elif item.age_hours <= lookback:
            score += 1

        item.score = score

    return sorted(items, key=lambda x: (x.score, x.published), reverse=True)


def relevance_verdict(item: Item, exclude_terms: Iterable[str]) -> tuple[bool, str]:
    """Decide whether an item belongs in the brief, and explain why.

    Two stages:

    1. Hard exclude — if any ``exclude_keywords`` term appears (whole word),
       drop the item outright. This catches agency-name collisions such as
       "Army-Navy game", "Old Navy" or an "MDA telethon".
    2. Primary mission signal — keep the item only if it names a tracked
       agency, a watchlist company, or comes from an authoritative .gov/.mil
       source. Event and capability keywords add score and tags but are too
       ambiguous to qualify a story on their own (e.g. "awarded", "BPA"),
       so they never pass the gate by themselves.

    Returns (keep, reason).
    """
    text = f"{item.title} {item.summary}".lower()
    for term in exclude_terms:
        if keyword_in(text, term):
            return False, f"excluded keyword: {term}"
    if item.agencies or item.companies:
        signal = ", ".join(item.agencies + item.companies)
        return True, f"mission signal: {signal}"
    domain = item.domain
    if domain.endswith(".gov") or domain.endswith(".mil"):
        return True, f"authoritative source: {domain}"
    return False, "no mission signal (no agency, company, or .gov/.mil source)"


def is_relevant(item: Item, exclude_terms: Iterable[str]) -> bool:
    return relevance_verdict(item, exclude_terms)[0]


def within_lookback(items: Iterable[Item], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [i for i in items if i.published >= cutoff]


def build_id() -> str:
    """Short identifier of the code that produced this brief, so a reader can
    tell a fresh brief from a stale one. Prefers the CI commit SHA; falls back
    to the local git HEAD, then "local"."""
    sha = os.getenv("GITHUB_SHA", "").strip()
    if sha:
        return sha[:7]
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "local"


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def tag_html(values: list[str]) -> str:
    return "".join(f'<span class="tag">{esc(v.replace("_", " "))}</span>' for v in values)


def md_lite_to_html(md: str) -> str:
    """Minimal, safe Markdown -> HTML for the LLM summary (escape first, then
    apply a small subset: bold, bullet lists, paragraphs). Avoids adding a
    Markdown dependency and never emits unescaped model output."""
    html_lines: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            continue
        safe = esc(line.strip())
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        if line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{safe[2:].lstrip()}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{safe}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


def render_html(items: list[Item], config: dict, out_path: Path, summary: str | None = None) -> None:
    b = config["brief"]
    top_n = int(b.get("top_items", 10))
    top = items[:top_n]
    rest = items[top_n:int(b.get("max_items", 45))]
    generated = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p %Z")
    build = build_id()

    def card(item: Item, rank: int | None = None) -> str:
        rank_html = f'<span class="rank">{rank}</span>' if rank else ""
        summary = item.summary[:550] + ("…" if len(item.summary) > 550 else "")
        tags = tag_html(item.agencies + item.companies + item.capabilities + item.events)
        return f"""
        <article class="card">
          <div class="headline">{rank_html}<a href="{esc(item.url)}">{esc(item.title)}</a></div>
          <div class="meta">{esc(item.source)} · {item.published.astimezone().strftime("%b %d, %I:%M %p")} · Relevance {item.score}</div>
          <div class="summary">{esc(summary)}</div>
          <div class="tags">{tags}</div>
        </article>"""

    top_html = "\n".join(card(x, i + 1) for i, x in enumerate(top))
    rest_html = "\n".join(card(x) for x in rest)

    counts = {
        "Navy/USMC": sum("Navy_Marine_Corps" in x.agencies for x in items),
        "Army": sum("Army" in x.agencies for x in items),
        "Air/Space": sum("Air_Force_Space_Force" in x.agencies for x in items),
        "SOCOM": sum("SOCOM" in x.agencies for x in items),
        "DHS": sum("Homeland_Security" in x.agencies for x in items),
        "Policy": sum("Policy_Regulation" in x.events for x in items),
        "Awards": sum("Award" in x.events for x in items),
        "Protests": sum("Protest" in x.events for x in items),
        "Watchlist": sum(bool(x.companies) for x in items),
    }
    chips = "".join(f'<div class="metric"><b>{v}</b><span>{esc(k)}</span></div>' for k, v in counts.items())

    summary_html = ""
    if summary:
        summary_html = (
            '<section class="pulse"><h2>Market Pulse</h2>'
            '<div class="pulse-badge">AI-generated from the filtered signals below · verify at source</div>'
            f'{md_lite_to_html(summary)}</section>'
        )

    doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{esc(b["title"])}</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;background:#f5f6f8;color:#17202a;margin:0}}
.wrap{{max-width:1100px;margin:auto;padding:28px}}
header{{background:#17202a;color:white;padding:28px;border-radius:14px}}
h1{{margin:0 0 6px;font-size:28px}} .sub{{opacity:.8}}
.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}}
.metric{{background:white;border:1px solid #ddd;border-radius:10px;padding:10px 14px;min-width:90px}}
.metric b{{display:block;font-size:22px}} .metric span{{font-size:12px;color:#5d6d7e}}
h2{{margin-top:28px}}
.card{{background:white;border:1px solid #e1e4e8;border-radius:12px;padding:16px 18px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}}
.headline{{font-size:18px;font-weight:700;line-height:1.35;display:flex;gap:10px}}
.headline a{{color:#163a5f;text-decoration:none}} .headline a:hover{{text-decoration:underline}}
.rank{{background:#17202a;color:white;border-radius:6px;min-width:28px;height:28px;text-align:center;line-height:28px}}
.meta{{font-size:12px;color:#6b7785;margin:7px 0}}
.summary{{font-size:14px;line-height:1.5}}
.tag{{display:inline-block;background:#edf2f7;border-radius:999px;padding:4px 8px;margin:7px 5px 0 0;font-size:11px}}
.pulse{{background:#eef4fb;border:1px solid #cfe0f2;border-left:5px solid #163a5f;border-radius:12px;padding:16px 20px;margin:18px 0}}
.pulse h2{{margin:0 0 6px;font-size:20px;color:#163a5f}}
.pulse-badge{{display:inline-block;background:#163a5f;color:white;font-size:11px;border-radius:999px;padding:3px 10px;margin-bottom:8px}}
.pulse p{{font-size:14px;line-height:1.55;margin:8px 0}}
.pulse ul{{margin:6px 0 6px 18px;padding:0}} .pulse li{{font-size:14px;line-height:1.5;margin:3px 0}}
footer{{font-size:12px;color:#6b7785;margin:30px 0}}
</style>
</head>
<body><div class="wrap">
<header><h1>{esc(b["title"])}</h1><div class="sub">Generated {esc(generated)} · build {esc(build)} · EPS-wide federal market awareness</div></header>
<div class="metrics">{chips}</div>
{summary_html}
<h2>Top Developments</h2>
{top_html or "<p>No qualifying items in the current window.</p>"}
<h2>More Worth Knowing</h2>
{rest_html or "<p>No additional qualifying items.</p>"}
<footer>Automated intelligence aid. Verify material facts at the linked primary source before capture, proposal, legal, or compliance decisions.</footer>
</div></body></html>"""
    out_path.write_text(doc, encoding="utf-8")


def render_markdown(items: list[Item], config: dict, out_path: Path, summary: str | None = None) -> None:
    b = config["brief"]
    lines = [
        f"# {b['title']}",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M %p %Z')} · build {build_id()}",
        "",
    ]
    if summary:
        lines += [
            "## Market Pulse",
            "",
            "_AI-generated from the filtered signals below · verify at source_",
            "",
            summary,
            "",
        ]
    lines += [
        "## Top Developments",
        "",
    ]
    for idx, i in enumerate(items[:int(b.get("max_items", 45))], 1):
        tags = ", ".join(i.agencies + i.companies + i.capabilities + i.events)
        lines += [
            f"{idx}. **[{i.title}]({i.url})**",
            f"   - Source: {i.source} | Relevance: {i.score} | Published: {i.published.astimezone().isoformat(timespec='minutes')}",
            f"   - Tags: {tags or 'General GovCon'}",
            f"   - {i.summary[:500]}",
            "",
        ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def send_email(subject: str, html_body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    to = os.getenv("EMAIL_TO", "").strip()
    sender = os.getenv("EMAIL_FROM", "").strip()
    if not host or not to or not sender:
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("Your EPS GovCon Morning Brief is attached in HTML format.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if user:
            s.login(user, password)
        s.send_message(msg)
    return True


def write_dropped_log(dropped: list[tuple[Item, str]], out_path: Path) -> None:
    """Write every filtered-out item with its reason, for auditing the gate."""
    lines = [
        "# Dropped items (filtered out of the brief)",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M %p %Z')}",
        f"Total dropped: {len(dropped)}",
        "",
    ]
    # Group by reason label (the part before any colon) for a quick tally.
    tally: dict[str, int] = {}
    for _, reason in dropped:
        label = reason.split(":")[0].strip()
        tally[label] = tally.get(label, 0) + 1
    lines.append("## Why items were dropped")
    lines.append("")
    for label, n in sorted(tally.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"- {n} — {label}")
    lines.append("")
    lines.append("## Items")
    lines.append("")
    for item, reason in sorted(dropped, key=lambda t: t[0].score, reverse=True):
        lines.append(f"- [{reason}] (score {item.score}) {item.title} — {item.source}")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def llm_executive_summary(items: list[Item], config: dict) -> str | None:
    """Generate a short GovCon-style 'market pulse' narrative over the filtered
    items using the Anthropic API. Grounded strictly in the items provided.

    Returns Markdown text, or None if disabled, no API key, the SDK is missing,
    the call fails, or the model declines. Never raises."""
    lcfg = config.get("llm", {})
    if not lcfg.get("enabled", False):
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[warn] LLM summary enabled but ANTHROPIC_API_KEY is not set; skipping.",
              file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print("[warn] anthropic package not installed; skipping LLM summary.", file=sys.stderr)
        return None

    top = items[: int(lcfg.get("max_items", 25))]
    if not top:
        return None

    lines = []
    for i, x in enumerate(top, 1):
        tags = ", ".join(x.agencies + x.companies + x.capabilities + x.events) or "General"
        lines.append(f"{i}. {x.title}\n   source: {x.source} | tags: {tags}\n   {x.summary[:300]}")
    context = "\n".join(lines)

    system = (
        "You are a government-contracting market analyst for EPS Corporation, an "
        "engineering and professional-services firm. You write a concise daily "
        "'market pulse' for capture and business-development staff.\n\n"
        "Rules:\n"
        "- Use ONLY the signals provided. Do not invent programs, dollar values, "
        "agencies, or awards that are not in the list. If something is uncertain, "
        "say so.\n"
        "- Be concise and factual. No preamble, no filler, no marketing tone.\n"
        "- Where useful, note briefly why an item matters to an EPS-type firm "
        "(engineering, software/IT, logistics, training, program support).\n"
        "- Output GitHub-flavored Markdown."
    )
    prompt = (
        "Here are today's filtered GovCon signals (already de-duplicated and "
        "relevance-ranked):\n\n"
        f"{context}\n\n"
        "Write the market pulse with these short sections (omit any section with "
        "nothing to report):\n"
        "**Market Pulse** — 2-3 sentences on the overall picture today.\n"
        "**Opportunities to watch** — sources sought / RFIs / forecasts.\n"
        "**Notable awards** — who won what.\n"
        "**Policy & regulation** — FAR/DFARS/CMMC/small-business changes.\n"
        "**Competitive moves** — watchlist companies.\n"
        "Keep the whole thing under ~300 words."
    )

    model = str(lcfg.get("model", "claude-sonnet-5"))
    max_tokens = int(lcfg.get("max_tokens", 2000))
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        print(f"[warn] LLM summary request failed: {exc}", file=sys.stderr)
        return None

    if getattr(resp, "stop_reason", None) == "refusal":
        print("[warn] LLM summary was declined by the model; skipping.", file=sys.stderr)
        return None

    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    return text or None


def main() -> int:
    ap = argparse.ArgumentParser(description="EPS-wide GovCon morning intelligence aggregator")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--email", action="store_true", help="Email the HTML brief using SMTP environment variables")
    args = ap.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    lookback = int(config["brief"].get("lookback_hours", 36))
    min_score = int(config["brief"].get("min_score", 2))

    # Time-sensitive feeds are held to the lookback window...
    print("Collecting Google News RSS...")
    items = google_news_items(config)
    print("Collecting Federal Register...")
    items += federal_register_items(config)
    items = within_lookback(items, lookback)

    # ...structured sources carry their own date bounds (posted/awarded within N
    # days), so they bypass the short news window and are appended after it.
    print("Collecting SAM.gov..." if config.get("sam", {}).get("enabled") else "SAM.gov collector disabled.")
    items += sam_items(config)
    print("Collecting GAO bid protests..." if config.get("gao_protests", {}).get("enabled") else "GAO protest collector disabled.")
    items += gao_protest_items(config)
    print("Collecting USAspending awards..." if config.get("usaspending", {}).get("enabled") else "USAspending collector disabled.")
    items += usaspending_items(config)

    items = dedupe(items)
    items = score_items(items, config)

    require_relevance = config["brief"].get("require_relevance", True)
    exclude = config["brief"].get("exclude_keywords", []) or []
    scored_total = len(items)

    kept: list[Item] = []
    dropped: list[tuple[Item, str]] = []
    for x in items:
        if x.score < min_score:
            dropped.append((x, f"below min_score: {x.score} < {min_score}"))
            continue
        if require_relevance:
            ok, reason = relevance_verdict(x, exclude)
            if not ok:
                dropped.append((x, reason))
                continue
        kept.append(x)
    items = kept

    # Audit summary to the console / Actions build log.
    print(f"Filter: kept {len(items)} of {scored_total} items "
          f"(dropped {len(dropped)}).")
    if items and config["brief"].get("log_kept", True):
        print("Kept items (what appears in the brief):")
        for x in items:
            tags = "/".join(x.agencies + x.companies) or "-"
            print(f"  score {x.score:>2} [{tags}] {x.title[:80]}")
    if dropped and config["brief"].get("log_dropped", True):
        tally: dict[str, int] = {}
        for _, reason in dropped:
            label = reason.split(":")[0].strip()
            tally[label] = tally.get(label, 0) + 1
        print("Dropped by reason:")
        for label, n in sorted(tally.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {n:>4}  {label}")
        preview = int(config["brief"].get("log_dropped_preview", 15))
        if preview > 0:
            print(f"Sample of dropped items (top {preview} by score):")
            for item, reason in sorted(dropped, key=lambda t: t[0].score, reverse=True)[:preview]:
                print(f"  - [{reason}] {item.title[:80]}")

    summary = None
    if config.get("llm", {}).get("enabled"):
        print("Generating LLM executive summary...")
        summary = llm_executive_summary(items, config)
        print("LLM summary generated." if summary else "LLM summary skipped.")

    out_dir = Path(config["brief"].get("output_dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    html_path = out_dir / f"EPS_GovCon_Brief_{stamp}.html"
    md_path = out_dir / f"EPS_GovCon_Brief_{stamp}.md"
    render_html(items, config, html_path, summary=summary)
    render_markdown(items, config, md_path, summary=summary)

    print(f"Wrote {html_path}")
    print(f"Wrote {md_path}")
    if dropped and config["brief"].get("log_dropped", True):
        dropped_path = out_dir / f"EPS_GovCon_Dropped_{stamp}.md"
        write_dropped_log(dropped, dropped_path)
        print(f"Wrote {dropped_path}")
    print(f"Selected {len(items)} relevant items.")

    if args.email:
        sent = send_email(config["brief"]["title"], html_path.read_text(encoding="utf-8"))
        print("Email sent." if sent else "Email not sent: SMTP environment variables are incomplete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
