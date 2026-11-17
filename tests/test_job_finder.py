from datetime import datetime, timedelta, timezone

import job_finder as jf

CFG = jf.load_config()
NOW = datetime(2026, 11, 17, 5, 0, tzinfo=timezone.utc)
DESC = "We are hiring a data analyst with Python and SQL skills. " * 5


def job(jid="1", title="Junior Data Analyst", desc=DESC, days_old=1, link="https://example.com/apply"):
    return {
        "job_id": jid, "job_title": title, "employer_name": "Acme", "job_city": "Riyadh",
        "job_is_remote": False, "job_description": desc, "job_apply_link": link,
        "job_posted_at_timestamp": int((NOW - timedelta(days=days_old)).timestamp()),
    }


def empty_state():
    return {"sent": {}, "maybe": {}}


def test_senior_titles_are_skipped():
    assert jf.is_senior(job(title="Senior Data Scientist"), CFG)
    assert jf.is_senior(job(title="Data Team Lead"), CFG)
    assert not jf.is_senior(job(title="Junior Data Analyst"), CFG)
    assert not jf.is_senior(job(title="Leading-edge AI Engineer Intern"), CFG)  # whole words only


def test_experience_filter():
    assert jf.needs_3plus_years(job(desc="Requires 3+ years of experience in SQL."))
    assert jf.needs_3plus_years(job(desc="Minimum 5 years experience."))
    assert jf.needs_3plus_years(job(desc="5-7 years of professional experience"))
    assert jf.needs_3plus_years(job(desc="خبرة لا تقل عن 3 سنوات"))
    assert not jf.needs_3plus_years(job(desc="0-2 years of experience, fresh graduates welcome."))
    assert not jf.needs_3plus_years(job(desc="Join our 300-person team."))


def test_verified_vs_maybe():
    assert jf.classify(job(), CFG, NOW) == (True, [])
    ok, missing = jf.classify(job(days_old=10), CFG, NOW)
    assert not ok and "recent posting date" in missing
    ok, missing = jf.classify(job(link=""), CFG, NOW)
    assert not ok and "apply link" in missing


def test_dedupe_and_maybe_streak():
    state = empty_state()
    state["sent"]["1"] = {}
    state["maybe"]["2"] = {"streak": 2, "last": "2026-11-16"}
    jobs = [job("1"), job("2", link=""), job("3")]
    ids = [c["id"] for c in jf.candidates(jobs, CFG, state, NOW)]
    assert ids == ["3"]


def test_keyword_scoring_and_render():
    cands = jf.candidates([job()], CFG, empty_state(), NOW)
    jf.score_keywords(cands, CFG)
    assert 0 <= cands[0]["score"] <= 10
    subject, body = jf.render(cands, [], ["Query 'x' failed"], "keywords", "2026-11-17")
    assert "(1 matches)" in subject and "Problems in this run" in body and "x&#x27; failed" in body


def test_fetch_survives_failed_query(monkeypatch):
    class Resp:
        def __init__(self, ok):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise jf.requests.HTTPError("403 Forbidden")

        def json(self):
            return {"data": {"jobs": [job("a"), job("a")]}}

    calls = iter([Resp(True), Resp(False), Resp(True)])
    monkeypatch.setattr(jf.requests, "get", lambda *a, **k: next(calls))
    jobs, errors = jf.fetch_jobs(CFG, "key")
    assert len(jobs) == 1 and len(errors) == 1


def test_full_dry_run(monkeypatch, tmp_path):
    """End to end with a fake API: nothing is sent and state is unchanged."""
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"jobs": [job("ok"), job("old", days_old=12), job("sr", title="Senior Data Scientist")]}}

    monkeypatch.setattr(jf.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(jf, "ROOT", tmp_path)
    monkeypatch.setattr(jf, "STATE_PATH", tmp_path / "sent_jobs.json")
    monkeypatch.setenv("RAPIDAPI_KEY", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(jf, "send_email", lambda *a: (_ for _ in ()).throw(AssertionError("sent in dry run")))
    assert jf.run(dry_run=True) == 0
    digest = (tmp_path / "digest.html").read_text()
    assert "Junior Data Analyst" in digest and "Senior" not in digest
    assert not (tmp_path / "sent_jobs.json").exists()


def test_claude_scoring_parses_reply(monkeypatch):
    import types

    reply = '[{"id": "1", "score": 8, "reason": "Strong SQL and Python match."}]'

    class Messages:
        def create(self, **kwargs):
            assert kwargs["model"] == CFG["model"]
            block = types.SimpleNamespace(type="text", text="Here you go:\n" + reply)
            return types.SimpleNamespace(content=[block], usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    fake = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=Messages()))
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)
    cands = jf.candidates([job("1")], CFG, empty_state(), NOW)
    assert jf.score(cands, CFG, "key", []) == "claude"
    assert cands[0]["score"] == 8 and cands[0]["reason"].startswith("Strong")
