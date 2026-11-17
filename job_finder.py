"""Daily Saudi Data/AI job finder.

Fetches recent jobs from the JSearch API (RapidAPI), filters out senior roles and
jobs already sent, scores the rest against a profile (Claude if an API key is set,
keyword matching otherwise), and emails a digest through Gmail SMTP.

Usage:
    python job_finder.py              # full run: fetch, score, email, save sent jobs
    python job_finder.py --dry-run    # no email, no state change; writes digest.html
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "sent_jobs.json"
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"
RIYADH = timezone(timedelta(hours=3))

log = logging.getLogger("job_finder")

# "3+ years", "minimum 3 years", "at least 5 years", "5-7 years", "3 سنوات", "خبرة 4 سنوات"
EXPERIENCE_RE = re.compile(
    r"(?:(?:minimum|min\.?|at least|over)\s*(?:of\s*)?([3-9]|1\d)\s*\+?\s*(?:years|yrs))"
    r"|(?:\b([3-9]|1\d)\s*(?:\+|plus|-\s*\d+)?\s*(?:years|yrs)(?:\s*of)?\s*(?:\w+\s+){0,3}experience)"
    r"|(?:\b([3-9]|1\d)\s*\+\s*(?:years|yrs))"
    r"|(?:([3-9]|1\d)\s*(?:سنوات|سنة))",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- config / state
def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"sent": {}, "maybe": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def env(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name, "").strip()
    if required and not value:
        sys.exit(f"Missing environment variable {name}. Add it as a GitHub Actions secret.")
    return value or None


# --------------------------------------------------------------------------- fetch
def fetch_jobs(cfg: dict, api_key: str) -> tuple[list[dict], list[str]]:
    """Run every query; return unique jobs and a list of error messages."""
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    jobs: dict[str, dict] = {}
    errors: list[str] = []
    logged_fields = False
    for query in cfg["queries"]:
        params = {"query": query, "country": cfg["country"], "date_posted": cfg["date_posted"], "num_pages": 1}
        try:
            r = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
            items = data.get("jobs", []) if isinstance(data, dict) else data
            log.info("query %r -> %d jobs", query, len(items))
            if items and not logged_fields:
                log.info("JSearch job fields: %s", sorted(items[0].keys()))
                logged_fields = True
            for j in items:
                if j.get("job_id"):
                    jobs.setdefault(j["job_id"], j)
        except (requests.RequestException, ValueError) as exc:
            msg = f"Query '{query}' failed: {exc}"
            log.warning(msg)
            errors.append(msg)
    return list(jobs.values()), errors


# --------------------------------------------------------------------------- filter
_RELATIVE_RE = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month)s?\b", re.IGNORECASE)
_UNIT_SECONDS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800, "month": 2592000}


def posted_at(job: dict, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    for key in ("job_posted_at_timestamp", "posted_at_timestamp"):
        ts = job.get(key)
        if ts:
            try:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                pass
    for key in ("job_posted_at_datetime_utc", "job_posted_at_datetime", "posted_at", "date_posted"):
        raw = job.get(key)
        if raw and isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    for key in ("job_posted_at", "posted_at", "job_posted_human_readable"):
        raw = job.get(key)
        if not raw or not isinstance(raw, str):
            continue
        text = raw.strip().lower()
        if "just now" in text or "today" in text:
            return now
        if "yesterday" in text:
            return now - timedelta(days=1)
        m = _RELATIVE_RE.search(text)
        if m:
            return now - timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    return None


COUNTRY_WORDS = ("saudi arabia", "ksa", "المملكة العربية السعودية", "السعودية")


def location(job: dict) -> str:
    return (job.get("job_city") or job.get("job_location") or job.get("job_state")
            or ("Remote" if job.get("job_is_remote") else "Saudi Arabia"))


def allowed_location(job: dict, cfg: dict) -> bool:
    allowed = cfg.get("allowed_locations")
    if not allowed or job.get("job_is_remote"):
        return True
    text = " ".join(str(job.get(k) or "") for k in ("job_city", "job_state", "job_location")).lower()
    stripped = text
    for w in COUNTRY_WORDS:
        stripped = stripped.replace(w, "")
    if not stripped.replace(",", "").strip():
        return True
    return any(a.lower() in text for a in allowed)


def is_senior(job: dict, cfg: dict) -> bool:
    title = (job.get("job_title") or "").lower()
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", title) for w in cfg["senior_title_words"])


def needs_3plus_years(job: dict) -> bool:
    return bool(EXPERIENCE_RE.search(job.get("job_description") or ""))


def classify(job: dict, cfg: dict, now: datetime) -> tuple[bool, list[str]]:
    """Return (verified, missing) using only data from the API."""
    missing = []
    when = posted_at(job, now)
    if not when:
        if cfg.get("date_posted") not in ("today", "3days", "week"):
            missing.append("posting date")
    elif now - when > timedelta(days=cfg["max_age_days"]):
        missing.append("recent posting date")
    if len((job.get("job_description") or "").strip()) < 100:
        missing.append("description")
    if not job.get("job_apply_link"):
        missing.append("apply link")
    return not missing, missing


def candidates(jobs: list[dict], cfg: dict, state: dict, now: datetime) -> list[dict]:
    out = []
    for j in jobs:
        jid = j["job_id"]
        if jid in state["sent"]:
            continue
        if is_senior(j, cfg) or needs_3plus_years(j) or not allowed_location(j, cfg):
            continue
        verified, missing = classify(j, cfg, now)
        maybe_days = state["maybe"].get(jid, {}).get("streak", 0)
        if not verified and maybe_days >= 2:
            continue  # a Maybe job is shown at most 2 days in a row
        when = posted_at(j, now)
        out.append({
            "id": jid,
            "title": j.get("job_title") or "",
            "company": j.get("employer_name") or j.get("company_name") or "Unknown company",
            "city": location(j),
            "remote": bool(j.get("job_is_remote")),
            "posted": when.astimezone(RIYADH).strftime("%Y-%m-%d") if when else "this week",
            "link": j.get("job_apply_link") or j.get("job_google_link") or "",
            "description": (j.get("job_description") or "")[: cfg["description_chars"]],
            "verified": verified,
            "missing": missing,
        })
    # verified first, then newest first; only the top N are scored to limit cost
    out.sort(key=lambda c: c["posted"] if c["posted"] != "this week" else "", reverse=True)
    out.sort(key=lambda c: not c["verified"])
    return out[: cfg["max_jobs_to_score"]]


# --------------------------------------------------------------------------- score
def score_keywords(cands: list[dict], cfg: dict) -> None:
    for c in cands:
        text = f"{c['title']} {c['description']}".lower()
        hits = [k for k in cfg["skills_keywords"] if k in text]
        title_bonus = 2 if re.search(r"data|machine learning|\bml\b|\bai\b|analyst|scientist", c["title"].lower()) else 0
        entry_bonus = 1 if re.search(r"junior|entry|graduate|intern|trainee|fresh", text) else 0
        # base 2: the job already matched a targeted search query
        c["score"] = min(10, 2 + len(hits) + title_bonus + entry_bonus)
        c["reason"] = f"Keyword match: {', '.join(hits[:5]) or 'none'}."


def score_with_claude(cands: list[dict], cfg: dict, api_key: str) -> None:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    payload = [{"id": c["id"], "title": c["title"], "company": c["company"], "city": c["city"],
                "description": c["description"]} for c in cands]
    prompt = (
        "Score how well each job matches this candidate, from 0 to 10, and give a one-sentence reason.\n\n"
        f"Candidate profile:\n{cfg['profile']}\n\n"
        f"Jobs (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        'Reply with JSON only: [{"id": "...", "score": 0, "reason": "..."}], one entry per job.'
    )
    msg = client.messages.create(model=cfg["model"], max_tokens=2000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    match = re.search(r"\[.*\]", text, re.DOTALL)
    results = {r["id"]: r for r in json.loads(match.group(0))} if match else {}
    for c in cands:
        r = results.get(c["id"])
        if r is None:
            raise ValueError(f"Claude returned no score for {c['id']}")
        c["score"] = max(0, min(10, int(r["score"])))
        c["reason"] = str(r["reason"]).strip()
    log.info("Claude usage: %s input, %s output tokens", msg.usage.input_tokens, msg.usage.output_tokens)


def score(cands: list[dict], cfg: dict, anthropic_key: str | None, errors: list[str]) -> str:
    if not cands:
        return "none"
    if anthropic_key:
        try:
            score_with_claude(cands, cfg, anthropic_key)
            return "claude"
        except Exception as exc:  # fall back rather than skip the digest
            log.warning("Claude scoring failed, using keywords: %s", exc)
            errors.append(f"Claude scoring failed ({exc}); keyword scoring used instead.")
    score_keywords(cands, cfg)
    return "keywords"


# --------------------------------------------------------------------------- email
def render(main: list[dict], maybe: list[dict], errors: list[str], method: str, today: str) -> tuple[str, str]:
    def row(c: dict, extra: str = "") -> str:
        loc = html.escape(c["city"] + (" (remote)" if c["remote"] and c["city"] != "Remote" else ""))
        return (
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'><b>{c['score']}/10</b></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'><a href='{html.escape(c['link'])}'>{html.escape(c['title'])}</a>"
            f"<br><span style='color:#555'>{html.escape(c['company'])} · {loc} · posted {c['posted']}</span>"
            f"<br><span style='color:#333'>{html.escape(c['reason'])}</span>{extra}</td>"
            "</tr>"
        )

    head = "<tr><th align='left' style='padding:8px'>Score</th><th align='left' style='padding:8px'>Job</th></tr>"
    parts = [f"<h2 style='font-family:sans-serif'>Saudi Data/AI jobs · {today}</h2><div style='font-family:sans-serif'>"]
    if main:
        parts.append(f"<table style='border-collapse:collapse;width:100%'>{head}{''.join(row(c) for c in main)}</table>")
    else:
        parts.append("<p>No fully verified matches today.</p>")
    if maybe:
        parts.append("<h3>Maybe (not fully verified)</h3>")
        parts.append("<table style='border-collapse:collapse;width:100%'>" + "".join(
            row(c, f"<br><i style='color:#a60'>Missing: {', '.join(c['missing'])}</i>") for c in maybe) + "</table>")
    if errors:
        parts.append("<p style='color:#a00'><b>Problems in this run:</b><br>" + "<br>".join(html.escape(e) for e in errors) + "</p>")
    parts.append(f"<p style='color:#888;font-size:12px'>Source: JSearch (Google for Jobs). Scoring: {method}.</p></div>")
    subject = f"Saudi Data/AI jobs {today} ({len(main)} matches)"
    return subject, "".join(parts)


def send_email(subject: str, body: str, sender: str, password: str, to: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(sender, password)
        s.sendmail(sender, [to], msg.as_string())


# --------------------------------------------------------------------------- main
def run(dry_run: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg, state = load_config(), load_state()
    now = datetime.now(timezone.utc)
    today = now.astimezone(RIYADH).strftime("%Y-%m-%d")

    jobs, errors = fetch_jobs(cfg, env("RAPIDAPI_KEY"))
    cands = candidates(jobs, cfg, state, now)
    method = score(cands, cfg, env("ANTHROPIC_API_KEY", required=False), errors)

    main = sorted([c for c in cands if c["verified"] and c["score"] >= cfg["min_score"]], key=lambda c: -c["score"])
    maybe = sorted([c for c in cands if not c["verified"] and c["score"] >= cfg["min_score"]],
                   key=lambda c: -c["score"])[: cfg["max_maybe"]]
    subject, body = render(main, maybe, errors, method, today)
    log.info("%d fetched, %d candidates, %d main, %d maybe", len(jobs), len(cands), len(main), len(maybe))

    if dry_run:
        (ROOT / "digest.html").write_text(body, encoding="utf-8")
        log.info("Dry run: wrote digest.html, nothing sent or saved. Subject: %s", subject)
        return 0

    sender = env("GMAIL_ADDRESS")
    send_email(subject, body, sender, env("GMAIL_APP_PASSWORD"), env("EMAIL_TO", required=False) or sender)

    for c in main:
        state["sent"][c["id"]] = {"title": c["title"], "company": c["company"], "date": today}
        state["maybe"].pop(c["id"], None)
    shown_maybe = {c["id"] for c in maybe}
    for c in maybe:
        prev = state["maybe"].get(c["id"], {})
        streak = prev.get("streak", 0) + 1 if prev.get("last") == (now - timedelta(days=1)).astimezone(RIYADH).strftime("%Y-%m-%d") else 1
        state["maybe"][c["id"]] = {"title": c["title"], "last": today, "streak": streak}
    for jid in list(state["maybe"]):
        if jid not in shown_maybe and state["maybe"][jid]["last"] != today:
            state["maybe"][jid]["streak"] = 0
    save_state(state)
    log.info("Email sent: %s", subject)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="write digest.html instead of emailing")
    sys.exit(run(parser.parse_args().dry_run))
