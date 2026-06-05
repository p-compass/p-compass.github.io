# Project Compass — Job Dashboard

A focused job aggregator for the team: customer support, telecalling/sales, and
backend-operations roles across India. Updates automatically every day at
**23:59 IST** and is published as a public web page anyone can open with the link.

**A Punch initiative.** Auto-retires **31 July 2026**.

---

## How it works

```
search-config.json   →   fetch_jobs.py (daily, via GitHub Actions)   →   index.html + jobs.json   →   GitHub Pages
   (what to search)        queries JSearch / Google for Jobs,              self-contained                (public link)
                           filters, dedupes, sorts newest-first            dashboard
```

- **Source:** [JSearch API](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) (Google for Jobs aggregator — legally indexes LinkedIn, foundit, TimesJobs, Naukri, employer pages).
- **Filters:** 3+ yrs experience, no freshers/interns, posted within 60 days.
- **Dashboard:** dark, mobile-first, filterable by experience / work-mode / city / source.

## Files

| File | Purpose |
|---|---|
| `search-config.json` | Role families, keywords, filters (edit to tune searches) |
| `fetch_jobs.py` | Daily engine: query → filter → dedupe → render (Python stdlib only, no deps) |
| `dashboard.html` | Design template (`__PAYLOAD__` injected at build time) |
| `index.html` | Generated public page (do not edit by hand) |
| `jobs.json` | Generated machine-readable job list |
| `.github/workflows/daily.yml` | The 23:59 IST scheduler |

## One-time setup

1. **Push this folder to a GitHub repo** (CVs are git-ignored and will not be uploaded).
2. **Add the API key as a secret:** repo → Settings → Secrets and variables → Actions →
   New repository secret → name `JSEARCH_API_KEY`, value = your RapidAPI key.
3. **Enable Pages:** repo → Settings → Pages → Source = *Deploy from a branch*,
   Branch = `main`, folder = `/ (root)`. Your public link appears there.
4. **First run:** Actions tab → *Daily job refresh* → *Run workflow*. After it finishes,
   open the Pages link.

## Analytics (Google Analytics 4)

Tracks **daily visits**, **visitor location/device** (aggregate, anonymous — no names),
and **Apply clicks per job & per source** (the `apply_click` event; `job_open` fires when
a card is opened). It's the best available proxy for "applied" — actual submission happens
on the third-party site and can't be measured.

**Setup:**
1. Create a free GA4 property at [analytics.google.com](https://analytics.google.com) →
   copy its **Measurement ID** (looks like `G-XXXXXXXXXX`).
2. Paste it into `search-config.json` → `analytics.ga_measurement_id`.
3. Next build injects the tag automatically. Leave the field blank to disable tracking.
4. *(Optional, for per-job/source breakdowns):* in GA4 → Admin → Custom definitions,
   register custom dimensions for event params `job_title`, `company`, `source`, `city`.
   Then "Apply clicks by source/job" become reportable.

Real-time visits appear in GA4 → Reports → Realtime. Apply clicks: Reports → Engagement → Events.

## Run locally

```bash
JSEARCH_API_KEY=your_key python3 fetch_jobs.py
# then open index.html in a browser
```

## Shutting it down (31 July 2026)

- Disable the workflow (Actions → *Daily job refresh* → Disable), and
- Settings → Pages → set Source to *None* (takes the site offline). Optionally make the repo private.
