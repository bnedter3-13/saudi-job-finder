# Saudi Data/AI Job Finder

![Tests](../../actions/workflows/tests.yml/badge.svg)

A daily job digest for entry-level **Data / ML / AI** roles in Saudi Arabia. It runs on GitHub Actions at 08:00 Riyadh time, at no infrastructure cost.

```
GitHub Actions (cron)
   │
   ▼
JSearch API (Google for Jobs) ──► 3 queries, country=sa, last 7 days
   │
   ▼
Filters ──► no senior titles · no "3+ years" · no jobs already sent
   │
   ▼
Scoring ──► Claude (0–10 + one-line reason), or keyword matching if no API key
   │
   ▼
Gmail ──► HTML digest: verified matches + "Maybe" section
   │
   ▼
data/sent_jobs.json committed back, so jobs never repeat
```

## Features

- **Verified vs Maybe.** A job counts as verified only when the API returns a posting date from the last 7 days, a description, and an apply link. Any other job goes to a short "Maybe" section that names what was missing, and it can appear there at most 2 days in a row.
- **Low cost.**
  - 3 API calls per day (about 90 a month; the JSearch free tier allows 200).
  - One batched Claude call per run.
  - At most 20 jobs are scored, with descriptions cut to 1,500 characters.
- **Fails safe.**
  - If a query fails, the email still goes out and lists the problem.
  - If Claude fails, keyword scoring is used instead.
- **Tested.** `pytest` covers the filters, deduplication, Maybe rules, scoring, and a full dry run with a mocked API.

## Setup

1. **Fork or upload** this repository to your GitHub account.
2. **Get the keys:**
   - **RapidAPI:** search for **JSearch** on <https://rapidapi.com>, subscribe to the Basic plan (free) and copy your key.
   - **Gmail:** turn on 2-Step Verification, then create an **App password** at <https://myaccount.google.com/apppasswords>. This is a 16-character password used only by this script.
   - **Anthropic (optional):** create an API key at <https://console.anthropic.com>. Without it, keyword scoring is used.
3. **Add secrets.** In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `RAPIDAPI_KEY` | your RapidAPI key |
   | `GMAIL_ADDRESS` | the Gmail address that sends the digest |
   | `GMAIL_APP_PASSWORD` | the 16-character app password |
   | `EMAIL_TO` | optional, where to send (defaults to `GMAIL_ADDRESS`) |
   | `ANTHROPIC_API_KEY` | optional |

4. **Run it once.** Go to **Actions → Daily job digest → Run workflow** and check your inbox. After that it runs every day by itself.

## Customise

Everything lives in `config.json`:
- `queries` and `profile`
- `min_score`
- `max_jobs_to_score` and `max_maybe`
- `senior_title_words`
- `model`

## Run locally

```bash
pip install -r requirements.txt
export RAPIDAPI_KEY=...            # Windows: set RAPIDAPI_KEY=...
python job_finder.py --dry-run     # writes digest.html, sends nothing, saves nothing
pytest -q
```

## Notes

- GitHub can start scheduled workflows a few minutes late. It also disables them after 60 days without commits; the commit of `sent_jobs.json` on days with new jobs usually keeps the schedule active.
- In a public repository, `data/sent_jobs.json` (job IDs, titles and companies) is public. Make the repository private if you prefer.
- Job data comes from JSearch (Google for Jobs). Always confirm a posting on the employer's page before applying.
