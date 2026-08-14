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


def is_relevant(item: Item, qualifying_events: set[str]) -> bool:
    """Gate that keeps the brief focused on federal contracting.

    An item qualifies only if it actually touches the mission: a tracked
    agency, a watchlist company, a hard contracting event (award, protest,
    acquisition signal, policy/regulation), or an authoritative government
    source. Generic capability or industry/budget keywords alone are not
    enough — that is what let sports and consumer-tech stories through.
    """
    if item.agencies or item.companies:
        return True
    if any(ev in qualifying_events for ev in item.events):
        return True
    domain = item.domain
    if domain.endswith(".gov") or domain.endswith(".mil"):
        return True
    return False


def within_lookback(items: Iterable[Item], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [i for i in items if i.published >= cutoff]


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def tag_html(values: list[str]) -> str:
    return "".join(f'<span class="tag">{esc(v.replace("_", " "))}</span>' for v in values)


def render_html(items: list[Item], config: dict, out_path: Path) -> None:
    b = config["brief"]
    top_n = int(b.get("top_items", 10))
    top = items[:top_n]
    rest = items[top_n:int(b.get("max_items", 45))]
    generated = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p %Z")

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

    doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
footer{{font-size:12px;color:#6b7785;margin:30px 0}}
</style>
</head>
<body><div class="wrap">
<header><h1>{esc(b["title"])}</h1><div class="sub">Generated {esc(generated)} · EPS-wide federal market awareness</div></header>
<div class="metrics">{chips}</div>
<h2>Top Developments</h2>
{top_html or "<p>No qualifying items in the current window.</p>"}
<h2>More Worth Knowing</h2>
{rest_html or "<p>No additional qualifying items.</p>"}
<footer>Automated intelligence aid. Verify material facts at the linked primary source before capture, proposal, legal, or compliance decisions.</footer>
</div></body></html>"""
    out_path.write_text(doc, encoding="utf-8")


def render_markdown(items: list[Item], config: dict, out_path: Path) -> None:
    b = config["brief"]
    lines = [
        f"# {b['title']}",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M %p %Z')}",
        "",
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


def main() -> int:
    ap = argparse.ArgumentParser(description="EPS-wide GovCon morning intelligence aggregator")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--email", action="store_true", help="Email the HTML brief using SMTP environment variables")
    args = ap.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    lookback = int(config["brief"].get("lookback_hours", 36))
    min_score = int(config["brief"].get("min_score", 2))

    print("Collecting Google News RSS...")
    items = google_news_items(config)
    print("Collecting Federal Register...")
    items += federal_register_items(config)
    print("Collecting SAM.gov..." if config.get("sam", {}).get("enabled") else "SAM.gov collector disabled.")
    items += sam_items(config)
    print("Collecting GAO bid protests..." if config.get("gao_protests", {}).get("enabled") else "GAO protest collector disabled.")
    items += gao_protest_items(config)

    items = within_lookback(items, lookback)
    items = dedupe(items)
    items = score_items(items, config)
    items = [x for x in items if x.score >= min_score]

    if config["brief"].get("require_relevance", True):
        qualifying = set(config["brief"].get(
            "qualifying_events",
            ["Acquisition_Signal", "Award", "Protest", "Policy_Regulation"],
        ))
        before = len(items)
        items = [x for x in items if is_relevant(x, qualifying)]
        print(f"Relevance gate: kept {len(items)} of {before} scored items "
              f"(dropped {before - len(items)} off-mission).")

    out_dir = Path(config["brief"].get("output_dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    html_path = out_dir / f"EPS_GovCon_Brief_{stamp}.html"
    md_path = out_dir / f"EPS_GovCon_Brief_{stamp}.md"
    render_html(items, config, html_path)
    render_markdown(items, config, md_path)

    print(f"Wrote {html_path}")
    print(f"Wrote {md_path}")
    print(f"Selected {len(items)} relevant items.")

    if args.email:
        sent = send_email(config["brief"]["title"], html_path.read_text(encoding="utf-8"))
        print("Email sent." if sent else "Email not sent: SMTP environment variables are incomplete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
