#!/usr/bin/env python3
"""
Project Compass — daily job fetch engine (Phase 2).

Reads search-config.json, queries the JSearch API (Google for Jobs aggregator,
covers LinkedIn / foundit / TimesJobs / Naukri / employer sites), applies the
brief's filters (3+ yrs, no freshers, posted within 60 days), dedupes across
publishers, sorts newest-first, and writes jobs.json for the dashboard.

Run:  JSEARCH_API_KEY=xxxx python3 fetch_jobs.py
Env:  JSEARCH_API_KEY   (required)  — RapidAPI key for jsearch.p.rapidapi.com
      JSEARCH_PAGES     (optional)  — pages per query (default 1)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "search-config.json"
OUTPUT_PATH = HERE / "jobs.json"

API_HOST = "jsearch.p.rapidapi.com"
API_URL = f"https://{API_HOST}/search"
USD_TO_INR = 83  # rough; only used when a listing reports salary in USD


# ----------------------------------------------------------------------------- helpers
def log(msg):
    print(f"[compass] {msg}", file=sys.stderr)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def all_queries(cfg):
    """Flatten every role-family query, honouring the fintech boost toggle."""
    queries = []
    for family, val in cfg["role_families"].items():
        if isinstance(val, list):
            queries.extend(val)
        elif isinstance(val, dict):  # fintech_broking_boost
            if val.get("enabled"):
                queries.extend(val.get("queries", []))
    # de-dupe while preserving order
    seen, out = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def call_jsearch(query, country, page, api_key):
    params = {
        "query": f"{query} in India",
        "page": str(page),
        "num_pages": "1",
        "country": country,
        "date_posted": "all",  # we filter to 60d ourselves (JSearch caps at 'month')
    }
    req = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": API_HOST},
    )
    with urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", []) or []


# ----------------------------------------------------------------------------- filtering
def title_excluded(title, patterns):
    t = (title or "").lower()
    return any(p.lower() in t for p in patterns)


def company_excluded(company, patterns):
    """True if the employer name looks like a staffing/recruitment agency."""
    c = (company or "").lower()
    return any(p.lower() in c for p in patterns)


def compile_words(words):
    """Word-boundary regex from a list (avoids 'plant' matching 'implementation'). None if empty."""
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def salary_min_monthly(display):
    """Lowest monthly ₹ implied by a formatted salary string, or None.
    Our formats: '₹9–14 LPA', '₹6 LPA', '₹20–35K/mo'. The min is the first number."""
    if not display or display == "Not disclosed":
        return None
    m = re.search(r"([\d.]+)", display)
    if not m:
        return None
    n = float(m.group(1))
    if "LPA" in display:
        return n * 100000 / 12      # lakhs/yr -> ₹/month
    if "K/mo" in display:
        return n * 1000             # thousands/month -> ₹/month
    return None


def passes_experience(job, min_years, exclude_no_exp):
    req = job.get("job_required_experience") or {}
    if exclude_no_exp and req.get("no_experience_required") is True:
        return False
    months = req.get("required_experience_in_months")
    if isinstance(months, (int, float)):
        return months >= min_years * 12
    # experience not reported -> keep (title-exclusions already removed freshers)
    return True


def within_days(ts, max_age_days):
    if not ts:
        return True  # keep undated rather than silently drop
    age = (datetime.now(timezone.utc).timestamp() - ts) / 86400
    return age <= max_age_days


def fmt_salary(job):
    lo, hi = job.get("job_min_salary"), job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").upper()
    cur = job.get("job_salary_currency") or "INR"
    if not lo and not hi:
        return "Not disclosed"
    factor = USD_TO_INR if cur == "USD" else 1
    vals = [v * factor for v in (lo, hi) if v]
    if period == "YEAR":
        nums = [f"{v/100000:.1f}".rstrip("0").rstrip(".") for v in vals]
        return "₹" + "–".join(nums) + " LPA"
    if period == "MONTH":
        nums = [f"{v/1000:.0f}" for v in vals]
        return "₹" + "–".join(nums) + "K/mo"
    return "Not disclosed"


# --- text parsing: JSearch rarely fills structured experience/salary for India,
#     but the description usually states both. Parse them out. ---
FRESHER_RE = re.compile(
    r"\b(freshers?|fresh graduate|no (?:prior )?experience|"
    r"0\s*[-–to]+\s*1\s*year|entry[\s-]?level|0\s*years?)\b", re.I)

_EXP_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),       # 3-5 years
    re.compile(r"(?:minimum|min\.?|at\s*least|atleast)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", re.I),                            # 3+ years
    re.compile(r"(\d{1,2})\s*(?:years|yrs|year)s?\s*(?:of\s*)?(?:experience|exp)", re.I),
    re.compile(r"(?:experience|exp)\s*[:\-]?\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
]


def parse_experience(text):
    """Minimum years of experience the role requires, or None if not stated."""
    if not text:
        return None
    if FRESHER_RE.search(text):
        return 0
    for pat in _EXP_PATTERNS:
        m = pat.search(text)
        if m:
            yrs = int(m.group(1))
            if 0 <= yrs <= 30:
                return yrs
    return None


_SAL_LPA_RANGE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,2}(?:\.\d{1,2})?)\s*[-–to]+\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:lpa|lakhs?|lacs?|lac)", re.I)
_SAL_LPA_ONE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:lpa|lakhs?|lacs?|lac)", re.I)
_SAL_MON_RANGE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{4,})\s*[-–to]+\s*(?:₹|rs\.?|inr)?\s*([\d,]{4,})\s*(?:/|per\s*)?\s*month", re.I)
_SAL_K_RANGE = re.compile(r"(\d{1,3})\s*[-–to]+\s*(\d{1,3})\s*k\s*(?:/|per\s*)?\s*month", re.I)


def _clean_num(s):
    return s.rstrip("0").rstrip(".") if "." in s else s


def parse_salary(text):
    """Format a salary range from description text, or None."""
    if not text:
        return None
    m = _SAL_LPA_RANGE.search(text)
    if m:
        return f"₹{_clean_num(m.group(1))}–{_clean_num(m.group(2))} LPA"
    m = _SAL_K_RANGE.search(text)
    if m:
        return f"₹{m.group(1)}–{m.group(2)}K/mo"
    m = _SAL_MON_RANGE.search(text)
    if m:
        lo, hi = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        return f"₹{lo//1000}–{hi//1000}K/mo"
    m = _SAL_LPA_ONE.search(text)
    if m:
        return f"₹{_clean_num(m.group(1))} LPA"
    return None


def normalize(job):
    city = job.get("job_city") or job.get("job_state") or ""
    remote = bool(job.get("job_is_remote"))
    desc = re.sub(r"\s+", " ", (job.get("job_description") or "")).strip()
    fulltext = (job.get("job_title") or "") + ". " + desc

    # experience: parsed-from-text first, then JSearch's structured field
    exp_years = parse_experience(fulltext)
    if exp_years is None:
        months = (job.get("job_required_experience") or {}).get("required_experience_in_months")
        if isinstance(months, (int, float)) and months:
            exp_years = int(round(months / 12))
    if exp_years is None and (job.get("job_required_experience") or {}).get("no_experience_required") is True:
        exp_years = 0  # explicit "no experience" -> treated as fresher, will be filtered

    # salary: structured first, then parsed-from-text
    salary = fmt_salary(job)
    if salary == "Not disclosed":
        salary = parse_salary(fulltext) or "Not disclosed"

    return {
        "id": job.get("job_id"),
        "title": (job.get("job_title") or "").strip(),
        "company": (job.get("employer_name") or "").strip(),
        "city": "Remote" if remote else (city or "India"),
        "mode": "remote" if remote else "office",
        "exp": exp_years,
        "salary": salary,
        "jd": desc[:240].rstrip() + ("…" if len(desc) > 240 else ""),
        "posted_ts": job.get("job_posted_at_timestamp") or 0,
        "posted_human": (job.get("job_posted_at_datetime_utc") or "")[:10],
        "source": job.get("job_publisher") or "Web",
        "url": job.get("job_apply_link") or "#",
    }


GA_SNIPPET = (
    "<!-- Google Analytics 4 -->\n"
    '<script async src="https://www.googletagmanager.com/gtag/js?id={id}"></script>\n'
    "<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}"
    "gtag('js',new Date());gtag('config','{id}');</script>"
)


def render_dashboard(payload, cfg=None):
    """Inject payload (+ optional GA4 tag) into dashboard.html -> self-contained index.html."""
    tpl_path = HERE / "dashboard.html"
    if not tpl_path.exists():
        log("note: dashboard.html template not found — skipping index.html render")
        return
    ga_id = ((cfg or {}).get("analytics") or {}).get("ga_measurement_id", "").strip()
    analytics = GA_SNIPPET.format(id=ga_id) if ga_id else ""
    if ga_id:
        log(f"analytics: GA4 enabled ({ga_id})")
    tpl = tpl_path.read_text(encoding="utf-8")
    html = (tpl
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
            .replace("__UPDATED_IST__", payload["updated_ist"])
            .replace("__ANALYTICS__", analytics))
    (HERE / "index.html").write_text(html, encoding="utf-8")
    log("rendered index.html")


def dedupe(jobs):
    """Drop exact job_id repeats and cross-publisher duplicates (title+company+city)."""
    seen_ids, seen_keys, out = set(), set(), []
    for j in jobs:
        key = (j["title"].lower(), j["company"].lower(), j["city"].lower())
        if j["id"] in seen_ids or key in seen_keys:
            continue
        seen_ids.add(j["id"])
        seen_keys.add(key)
        out.append(j)
    return out


# ----------------------------------------------------------------------------- main
def main():
    api_key = os.environ.get("JSEARCH_API_KEY")
    if not api_key:
        log("ERROR: set JSEARCH_API_KEY environment variable.")
        sys.exit(1)

    cfg = load_config()
    f = cfg["filters"]
    # pages: env var wins, else config's fetch.pages_per_query, else 1
    pages = int(os.environ.get("JSEARCH_PAGES")
                or (cfg.get("fetch") or {}).get("pages_per_query", 1))
    queries = all_queries(cfg)
    log(f"{len(queries)} queries × {pages} page(s) = up to {len(queries)*pages} API calls")

    raw, calls, errors = [], 0, 0
    for q in queries:
        for page in range(1, pages + 1):
            try:
                raw.extend(call_jsearch(q, f["country"], page, api_key))
            except (HTTPError, URLError, OSError) as e:  # OSError covers socket timeouts
                errors += 1
                log(f"  ! '{q}' p{page}: {e}")
            calls += 1
            time.sleep(0.25)  # stay under rate limits

    log(f"{calls} calls done ({errors} errored), {len(raw)} raw listings")

    kept = []
    dropped_exp = dropped_agency = dropped_low_sal = dropped_industrial = 0
    min_years = f["min_experience_years"]
    sal_floor = f.get("min_salary_per_month", 0)
    agency_pats = f.get("exclude_companies_containing", [])
    ind_title_rx = compile_words(f.get("exclude_industrial_titles", []))
    ind_phrase_rx = compile_words(f.get("exclude_industrial_jd_phrases", []))
    for job in raw:
        title = job.get("job_title", "")
        if title_excluded(title, f["exclude_titles_containing"]):
            continue
        if company_excluded(job.get("employer_name", ""), agency_pats):
            dropped_agency += 1
            continue
        # drop manufacturing/plant/engineering roles: industrial word in TITLE,
        # or a strong industrial phrase in the DESCRIPTION
        if (ind_title_rx and ind_title_rx.search(title)) or \
           (ind_phrase_rx and ind_phrase_rx.search(job.get("job_description") or "")):
            dropped_industrial += 1
            continue
        if not within_days(job.get("job_posted_at_timestamp"), f["max_age_days"]):
            continue
        norm = normalize(job)
        # 3-yr floor: drop only when experience is KNOWN and below the floor
        if norm["exp"] is not None and norm["exp"] < min_years:
            dropped_exp += 1
            continue
        # salary floor: drop only when salary is STATED and below floor (keep undisclosed)
        sal_min = salary_min_monthly(norm["salary"])
        if sal_floor and sal_min is not None and sal_min < sal_floor:
            dropped_low_sal += 1
            continue
        kept.append(norm)

    kept = dedupe(kept)
    kept.sort(key=lambda j: j["posted_ts"], reverse=True)  # newest first

    now = datetime.now(timezone.utc)
    ist = now + timedelta(hours=5, minutes=30)
    payload = {
        "updated_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "updated_ist": ist.strftime("%d %b %Y, %H:%M IST"),
        "count": len(kept),
        "jobs": kept,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as out:
        json.dump(payload, out, ensure_ascii=False, indent=2)
    with_exp = sum(1 for j in kept if j["exp"] is not None)
    with_sal = sum(1 for j in kept if j["salary"] != "Not disclosed")
    log(f"wrote {len(kept)} jobs -> {OUTPUT_PATH.name} (of {len(raw)} raw; "
        f"dropped: {dropped_agency} agency, {dropped_industrial} industrial, "
        f"{dropped_exp} <{min_years}yr, {dropped_low_sal} <₹{sal_floor//1000}K/mo; "
        f"{with_exp} have exp, {with_sal} have salary)")

    render_dashboard(payload, cfg)


if __name__ == "__main__":
    main()
