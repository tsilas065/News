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

## Recommended next upgrades

The starter build is deliberately simple and maintainable. The strongest next additions are:

1. **Company/competitor watchlists** — score named primes, teammates and incumbents.
2. **Program/vehicle watchlists** — SeaPort NxG, GSA MAS, OASIS+, CIO-SP4, Alliant, agency IDIQs, etc.
3. **GAO protest collector** — dedicated protest docket monitoring.
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

- `govcon_brief.py` — collector, deduplication, scoring, report generation, optional email
- `config.yaml` — EPS-wide agencies, capabilities, event types, queries and weights
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable names
