# EPS Corporation GovCon Morning Intelligence Brief

A configurable Python news/intelligence collector for **EPS-wide federal market awareness**.

It is intentionally broader than a single agency or division. The default configuration watches:

- Navy / Marine Corps
- Army
- Air Force / Space Force
- Defense agencies
- USSOCOM / SOF
- Department of Homeland Security, including USCG, CISA, CBP, FEMA and TSA
- Selected civilian federal agencies
- Acquisition signals, awards, protests, policy/FAR/DFARS/CMMC, budgets, industry moves
- EPS-relevant capability themes: engineering/prototyping, fabrication/maintenance, software/IT/cyber, logistics/lifecycle, training/AV, and program/business support

## What it produces

Each run creates:

- `output/EPS_GovCon_Brief_YYYY-MM-DD.html` — readable morning dashboard
- `output/EPS_GovCon_Brief_YYYY-MM-DD.md` — Markdown version for Teams/SharePoint/notes

The report scores and tags stories by:

1. customer/agency relevance,
2. EPS capability relevance,
3. event type,
4. source authority,
5. recency.

## Data sources in this starter build

### 1. Industry/news articles
Google News RSS searches configured in `config.yaml`. This is useful for discovering public reporting from GovCon, defense, federal IT and mainstream sources without maintaining fragile site-by-site scrapers.

### 2. Federal Register
Uses the public Federal Register API for acquisition, procurement, cyber, small-business and related regulatory updates.

### 3. SAM.gov (optional)
The SAM collector is included but **disabled by default**. To use it:

1. Obtain a SAM.gov public API key.
2. Set `SAM_API_KEY`.
3. Change `sam.enabled` to `true` in `config.yaml`.

Do not replace this with HTML scraping of SAM.gov.

### 4. GAO bid protests
Pulls GAO bid-protest decisions directly from GAO's public RSS feed, so
protests are captured at the source instead of only when news happens to
report them. Configured under `gao_protests` in `config.yaml`:

```yaml
gao_protests:
  enabled: true
  feeds:
    - "https://www.gao.gov/rss/bid_protests.xml"
```

These items are pre-tagged as `Protest` events and receive the `gao.gov`
source boost. If GAO changes the feed path, update the URL here — no code
change is required.

## Keeping the brief on-topic

Two mechanisms keep sports, consumer tech, and generic business stories out:

1. **Whole-word keyword matching.** Keywords match only as complete tokens, so
   acronyms never match inside unrelated words — `SOF` no longer matches
   "**sof**tware", `AI` no longer matches "tr**ai**ning", `FAR` no longer
   matches "D**FAR**S", and `PEO` no longer matches "**peo**ple".

2. **Relevance gate** (`require_relevance: true`). After scoring, an item is
   kept only if it carries a **primary mission signal**:

   - a tracked **agency** match (Navy, Army, DHS, …),
   - a **watchlist company** match, or
   - an authoritative **`.gov` / `.mil` source**.

   Event and capability keywords (awarded, software, AI, engineering, …) add
   score and tags but are **too ambiguous to qualify a story on their own** —
   that is what previously let "Taylor Swift *awarded*…" or a *BPA*-free bottle
   review through. They now only re-rank items that already passed the gate.

3. **Exclusion list** (`exclude_keywords`). Some agency names collide with
   everyday content — "Army-Navy game", "Old Navy", "Salvation Army", an "MDA
   telethon". Any item containing an exclusion term (whole word) is dropped
   even if it matched an agency. Grow this list whenever you spot noise.

Tuning knobs in `config.yaml` under `brief`:

```yaml
min_score: 5              # raise to be stricter, lower to widen
require_relevance: true   # set false to see everything that clears min_score
exclude_keywords:         # hard-drop these whole words (add as you spot noise)
  - "college football"
  - "Old Navy"
  - "telethon"
```

Each run prints how many items the gate dropped, so you can see the effect. If a
real item ever gets filtered out, add the missing agency, program, or company
term to the relevant group — and note that highly ambiguous acronyms (e.g. bare
`NSW`, `MDA`, `DHA`, `BPA`, `FAR`) were removed or replaced with full names/
phrases to prevent exactly this kind of collision.

### Auditing what was filtered out

With `log_dropped: true` (default), every run tells you exactly what it removed
and why — so you can spot-check the gate instead of catching leaks by eye:

- **Console / Actions log:** a tally by reason plus a sample of the highest-
  scoring dropped items, e.g.

  ```
  Filter: kept 12 of 137 items (dropped 125).
  Dropped by reason:
     118  below min_score
       6  no mission signal (no agency, company, or .gov/.mil source)
       1  excluded keyword: Old Navy
  Sample of dropped items (top 15 by score):
    - [excluded keyword: Old Navy] Old Navy launches fall collection ...
  ```

- **Full file:** `output/EPS_GovCon_Dropped_YYYY-MM-DD.md` lists every dropped
  item with its reason and score.

When you see an off-topic item in the brief, find it in this list — the reason
tells you which keyword let it in (or that it slipped through on a real agency
name, meaning it needs an `exclude_keywords` entry). Set `log_dropped: false` to
turn the audit trail off.

## Company / competitor watchlist

`company_groups` in `config.yaml` scores and tags stories that name primes,
competitors, teammates or incumbents you care about. It works exactly like the
agency and capability groups:

```yaml
company_groups:
  Teammates_Incumbents:
    weight: 4
    keywords:
      - "SimVentions"
  Primes_Competitors:
    weight: 3
    keywords:
      - "Leidos"
      - "CACI"
```

Matched companies appear as tags on each card and drive the **Watchlist**
metric chip in the dashboard header. The shipped names are examples — edit them
to reflect your real pursuit landscape.

## Install

### Windows PowerShell

```powershell
cd eps_govcon_morning_brief
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python govcon_brief.py
```

### macOS / Linux

```bash
cd eps_govcon_morning_brief
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python govcon_brief.py
```

Open the generated HTML file in `output/`.

## Tune it for EPS

You normally only edit `config.yaml`.

### Add a customer

Under `agency_groups`, add:

```yaml
New_Customer:
  weight: 5
  keywords:
    - "Full Agency Name"
    - "Command Acronym"
    - "Program Office"
```

### Add a capability

Under `capability_groups`:

```yaml
New_Capability:
  weight: 3
  keywords:
    - "keyword one"
    - "keyword two"
```

### Add a focused article feed

Under `news_queries`:

```yaml
- name: "My Focus"
  query: '"exact phrase" OR acronym contract acquisition'
```

## Email delivery

Set the SMTP values shown in `.env.example` as environment variables, then run:

```bash
python govcon_brief.py --email
```

The script does not load `.env` automatically so credentials are not accidentally read from files. Use operating-system environment variables or a secrets manager.

## Schedule every weekday morning

### Windows Task Scheduler

Create a Basic Task:

- Trigger: Weekly, Monday-Friday, e.g. 6:00 AM
- Program: path to your virtual environment's `python.exe`
- Arguments: full path to `govcon_brief.py`
- Start in: full path to this project directory

### Linux/macOS cron example

```cron
0 6 * * 1-5 cd /path/to/eps_govcon_morning_brief && /path/to/.venv/bin/python govcon_brief.py
```

### GitHub Actions + GitHub Pages (hosted, no local run)

`.github/workflows/morning-brief.yml` runs the brief on GitHub's runners every
weekday morning and publishes the dashboard to GitHub Pages, so you get a
stable URL without running anything locally.

One-time setup:

1. In the repo, go to **Settings → Pages** and set **Source: GitHub Actions**
   (the workflow attempts to enable this automatically on first run).
2. Optionally add a **`SAM_API_KEY`** repository secret (Settings → Secrets and
   variables → Actions) and set `sam.enabled: true` in `config.yaml`.
3. Trigger a run: **Actions → EPS GovCon Morning Brief → Run workflow**, or wait
   for the 11:00 UTC weekday schedule.

The published dashboard lives at `https://<your-user>.github.io/<repo>/`
(for this repo, `https://tsilas065.github.io/News/`). Each run also archives the
dated `EPS_GovCon_Brief_YYYY-MM-DD.html` / `.md` files alongside `index.html`.

Adjust the schedule by editing the `cron` line in the workflow (it is in UTC;
`0 11 * * 1-5` is roughly 6–7 a.m. US Eastern).

## Recommended next upgrades

The starter build is deliberately simple and maintainable. The strongest next additions are:

1. ~~**Company/competitor watchlists** — score named primes, teammates and incumbents.~~ ✅ Implemented (`company_groups`).
2. **Program/vehicle watchlists** — SeaPort NxG, GSA MAS, OASIS+, CIO-SP4, Alliant, agency IDIQs, etc.
3. ~~**GAO protest collector** — dedicated protest docket monitoring.~~ ✅ Implemented (`gao_protests`).
4. **DoD daily contracts parser** — structured extraction of awards by branch, amount and contractor.
5. **USAspending integration** — incumbent/award trend context.
6. **SharePoint/Teams delivery** — post the brief internally.
7. **Feedback learning** — a "useful/not useful" file that automatically tunes ranking.
8. **LLM executive summary** — optional AI-generated "Why EPS should care" notes after the deterministic filters have reduced noise.

## Security / compliance notes

- Keep API keys and mail passwords out of source control.
- Use public APIs and feeds where available.
- Do not scrape sites whose terms prohibit automated scraping.
- Treat generated relevance scores as triage, not authoritative legal/acquisition analysis.
- Verify high-impact items at the linked primary source.

## Files

- `govcon_brief.py` — collectors (news, Federal Register, SAM.gov, GAO protests), deduplication, scoring, report generation, optional email
- `config.yaml` — EPS-wide agencies, companies, capabilities, event types, queries and weights
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable names
- `.github/workflows/morning-brief.yml` — scheduled build + GitHub Pages publish
