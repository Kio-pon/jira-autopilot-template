#!/usr/bin/env python3
"""Tests for jira-autopilot. Standard library only, no Jira account needed.

  python3 tests/run_tests.py            parser and unit tests, then 30-day simulations
  python3 tests/run_tests.py --quick    parser and unit tests, then one 14-day simulation
  python3 tests/run_tests.py --seed 5 --days 45 --verbose    one longer run, with a report

The simulation replays GitHub's half-hourly cron (with its random delays and skipped runs)
against a fake Jira, adds a person who sometimes edits Jira and sometimes files real tasks
through issues, then checks the board history looks like a person and nothing unsafe happened.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
from fakejira import TZ, Clock, FakeJira, S01, S02, S03, S04, S05, S07, S08  # noqa: E402

UTC = timezone.utc
REAL_LINES = ["Finish DBMS normalization worksheet | 2h | fri", "Call the bank about the card | 10m | today",
              "Prepare SEPM presentation | 4h | in 5 days", "Read OS chapter 4 | 1h30m | tmrw",
              "Submit DCN lab report | 3h | mon", "Email the FYP supervisor | 15m | today",
              "Fix Robocat servo wiring | 2h | sat", "Revise DCN subnetting | 1h | wed",
              "Book the dentist | 10m | tomorrow", "Draft Voltrix pitch deck | 5h | next week"]


def load_autopilot():
    spec = importlib.util.spec_from_file_location("autopilot", os.path.join(ROOT, "autopilot.py"))
    ap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ap)
    return ap


# ---------------------------------------------------------------- parser tests

def test_parser():
    ap = load_autopilot()

    class C:
        def today(self):
            return datetime(2026, 10, 17).date()  # a Saturday

        def day(self, n=0):
            return (self.today() + timedelta(days=n)).isoformat()
    c = C()
    cases = [
        ("Finish DB lab report | 2h | fri", ("Finish DB lab report", 120, "2026-10-23", None)),
        ("Call the bank about the card | 10m | today", ("Call the bank about the card", 10, "2026-10-17", None)),
        ("Prepare SEPM presentation | 4h | 24 oct | SEPM", ("Prepare SEPM presentation", 240, "2026-10-24", "SEPM")),
        ("Read chapter 3 | 1h30m | tmrw", ("Read chapter 3", 90, "2026-10-18", None)),
        ("Buy groceries", ("Buy groceries", None, None, None)),
        ("Finish DB lab by friday, 2h", ("Finish DB lab", 120, "2026-10-23", None)),
        ("Submit form (30 min) tomorrow", ("Submit form", 30, "2026-10-18", None)),
        ("Watch OS lecture 1.5 hours on mon", ("Watch OS lecture", 90, "2026-10-19", None)),
        ("Pay fee | 15 min | 2026-11-02", ("Pay fee", 15, "2026-11-02", None)),
        ("Plan trip | | oct 10", ("Plan trip", None, "2027-10-10", None)),
        ("Meet Sara | sat", ("Meet Sara", None, "2026-10-17", None)),
        ("Revise for quiz in 3 days", ("Revise for quiz", None, "2026-10-20", None)),
        ("Sunday market run", ("Sunday market run", None, None, None)),
    ]
    bad = [(line, ap.split_line(c, line), want) for line, want in cases if ap.split_line(c, line) != want]
    assert not bad, "parser: " + "; ".join(f"{l!r} gave {g} not {w}" for l, g, w in bad)
    assert [ap.points_for(x) for x in (10, 15, 45, 60, 120, 240, 600)] == [1, 1, 2, 2, 3, 5, 8]
    assert ap.day_off({"days_off": ["2026-12-20..2027-01-02"]}, datetime(2026, 12, 25).date())
    assert not ap.day_off({"days_off": ["2026-12-20..2027-01-02"]}, datetime(2027, 1, 3).date())
    pool = ap.Pool({}, {"courses": [{"name": "Database Systems", "short": "DB"}],
                        "projects": [{"name": "Robocat"}]}, [])
    assert [pool.kind(a) for a in ("DB", "Database Systems", "robocat", "chores")] == \
        ["course", "course", "project", "chores"], "courses must match by short name too"
    assert pool.short("DB") == "DB" and pool.short("Database Systems") == "DB"
    t = ap.typo("Revise lecture notes")
    assert t and t != "Revise lecture notes" and sorted(t) == sorted("Revise lecture notes")
    return "parser: 13 lines, points, days off, typos"


# ---------------------------------------------------------------- unit tests

def fresh_env(**kw):
    keep = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return keep


def restore_env(keep):
    for k, v in keep.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def run_main(ap, argv):
    old = sys.argv
    sys.argv = ["autopilot.py"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ap.main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_alerts():
    with tempfile.TemporaryDirectory() as tmp:
        alert, healthy = os.path.join(tmp, "alert.md"), os.path.join(tmp, "ok")
        keep = fresh_env(JIRA_SITE="x.atlassian.net", JIRA_EMAIL="a@b.c", JIRA_TOKEN="t", JIRA_PROJECT="PER",
                         ALERT_FILE=alert, HEALTHY_FILE=healthy, GEMINI_API_KEY=None)
        try:
            ap = load_autopilot()
            clock = Clock(datetime(2026, 9, 7, 6, 0, tzinfo=UTC))
            ap.CLOCK, ap.SLEEP = clock, clock.advance
            fake = FakeJira(ap, clock, "sepm")
            ap.Jira = lambda *a, **k: fake
            base_store = ap.Store
            ap.Store = lambda j, acc, dry, path=None: base_store(j, acc, dry, None)

            fake.fail_myself = 401
            run_main(ap, ["tick"])  # an expired token: one alert, no crash
            text = open(alert).read()
            assert "token" in text.lower() and "JIRA_TOKEN" in text, text
            os.remove(alert)

            fake.fail_myself = None
            run_main(ap, ["check"])
            assert os.path.exists(healthy) and not os.path.exists(alert), "a clean run should report healthy"
            os.remove(healthy)

            real_run = ap.run

            def broken(ctx, command, count=None, store=None):
                raise ap.JiraError(500, "GET /rest/api/3/search/jql -> 500 boom")
            ap.run = broken
            for n in range(1, 4):
                run_main(ap, ["tick"])
                assert os.path.exists(alert) == (n >= 3), f"alert should appear on the third failure, not run {n}"
            assert "boom" in open(alert).read()
            os.remove(alert)

            ap.run = real_run
            run_main(ap, ["check"])
            assert os.path.exists(healthy), "recovery should report healthy"
            state = json.loads(fake.props["/rest/api/3/user/properties/jira-autopilot-state"])
            assert state["errors_in_row"] == 0
        finally:
            restore_env(keep)
    return "alerts: expired token, three failures in a row, recovery"


def test_search_lag():
    """Jira's search can trail a change by a few seconds. A night must not start task after task
    because a search still shows nothing in progress, and a dry run must not loop forever."""
    ap = load_autopilot()
    clock = Clock(datetime(2026, 9, 7, 6, 0, tzinfo=UTC))
    ap.CLOCK, ap.SLEEP = clock, clock.advance
    fake = FakeJira(ap, clock, "plain")
    cfg = ap.deep_merge(ap.DEFAULTS, {"project_key": "PER"})
    st = {"meta": {}}
    ctx = ap.Ctx(cfg, fake, st, {}, {})
    for n in range(8):
        k = fake._new("Task", f"Study chapter {n}")
        fake.issues[k]["duedate"] = "2026-09-08"
        st["meta"][k] = {"a": "self", "s": "b", "p": 3}
    fake.lag = True  # searches keep showing the old statuses for the whole run
    fake.lagged = {k: dict(i) for k, i in fake.issues.items()}
    with contextlib.redirect_stdout(io.StringIO()):
        ap.phase_night_work(ctx, 5.0)
    doing = [k for k, i in fake.issues.items() if i["status"] == "In Progress"]
    assert len(doing) <= cfg["wip_limit"], f"{len(doing)} tasks started while search lagged"
    fake.dry, fake.calls, fake.lag = True, 0, False
    for k in doing:  # back to nothing in progress, so the dry run has to start something
        fake.issues[k]["status"] = "To Do"
    ctx2 = ap.Ctx(cfg, fake, st, {}, {})
    with contextlib.redirect_stdout(io.StringIO()):
        ap.phase_night_work(ctx2, 5.0)  # must return, not loop
    assert all(i["status"] != "In Progress" for i in fake.issues.values()), "a dry run changed Jira"
    return f"search lag: {len(doing)} started with a frozen index, dry run finishes"


def test_setup():
    with tempfile.TemporaryDirectory() as tmp:
        tasks_file, result = os.path.join(tmp, "tasks.json"), os.path.join(tmp, "result.md")
        keep = fresh_env(GEMINI_API_KEY="fake", TASKS_FILE=tasks_file, INBOX_RESULT=result,
                         ISSUE_BODY="3rd year CS student. Courses: Database Systems (DB), Operating Systems (OS). "
                                    "Project: Robocat. I play futsal on weekends.")
        try:
            ap = load_autopilot()
            clock = Clock(datetime(2026, 9, 7, 6, 0, tzinfo=UTC))
            ap.CLOCK, ap.SLEEP = clock, clock.advance
            fake = FakeJira(ap, clock, "sepm")
            seen = {}

            def gemini(prompt, model, shape="["):
                seen["prompt"], seen["model"] = prompt, model
                return {"profile": "CS student", "courses": [{"name": "Database Systems", "short": "DB"}],
                        "projects": [{"name": "Robocat", "short": "Robocat"}],
                        "tasks": [[f"Practise DB query set {n}", "b" if n % 3 else "q", "Database Systems"]
                                  for n in range(40)] + [["Practise DB query set 1", "b", "x"]],
                        "urgent": [["DB lab due tonight", "b", "Database Systems"]]}
            ap.llm_json = gemini
            cfg = ap.deep_merge(ap.DEFAULTS, {"project_key": "PER"})
            st = {}
            ctx = ap.Ctx(cfg, fake, st, {}, {})
            with contextlib.redirect_stdout(io.StringIO()):
                ap.run(ctx, "setup")
            data = json.load(open(tasks_file))
            assert len(data["tasks"]) == 40, "duplicates should be dropped"
            assert data["urgent"] and data["courses"][0]["short"] == "DB"
            assert "MIDAS" in seen["prompt"] and seen["model"] == "gemini-3.8-flash", "prompt should carry the epics"
            assert "Saved tasks.json with 40 tasks" in open(result).read()
        finally:
            restore_env(keep)
    return "setup: tasks.json from an issue, duplicates dropped, epics in the prompt"


# ---------------------------------------------------------------- simulation

class Sim:
    def __init__(self, seed, kind, days, personal=True):
        self.ap = ap = load_autopilot()
        ap.rng.seed(seed)
        self.r = random.Random(seed * 7919 + 1)
        self.kind, self.days = kind, days
        self.t0 = datetime(2026, 9, 6, 22, 0, tzinfo=UTC)  # Monday 7 Sep, 03:00 Pakistan time
        self.clock = Clock(self.t0)
        ap.CLOCK, ap.SLEEP = self.clock, self.clock.advance
        self.jira = FakeJira(ap, self.clock, kind)
        cfg = ap.deep_merge(ap.DEFAULTS, ap.load_json(os.path.join(ROOT, "config.json"), {}))
        cfg["project_key"] = "PER"
        self.cfg = cfg
        self.generic = ap.load_json(os.path.join(ROOT, "generic_tasks.json"), {})
        self.personal = ap.load_json(os.path.join(ROOT, "tasks.example.json"), {}) if personal else {}
        self.logs = []
        self.real, self.asked_done, self.urgent = set(), set(), set()
        self.human_days = set()
        self.snapshots = []  # (local day, inbox ages in days, active sprint?, bot WIP)

    def local(self, t=None):
        return (t or self.clock()).astimezone(TZ)

    def session(self, command, env=None):
        ap = self.ap
        keep = fresh_env(GEMINI_API_KEY=None, **(env or {}))
        buf = io.StringIO()
        try:
            me = self.jira.get("/rest/api/3/myself")
            store = ap.Store(self.jira, me["accountId"], False, None)
            st = store.load()
            with contextlib.redirect_stdout(buf):
                ctx = ap.Ctx(self.cfg, self.jira, st, self.generic, self.personal)
                try:
                    ap.run(ctx, command, None, store)
                finally:
                    store.save(st)
        finally:
            restore_env(keep)
            self.logs.append((self.clock(), command, buf.getvalue()))
        return buf.getvalue(), st

    def timeline(self):
        events = []
        for d in range(self.days):
            utc_day = datetime(2026, 9, 7, tzinfo=UTC) + timedelta(days=d)
            for h in range(3, 20):  # the workflow's cron: hourly 03-15 UTC, then every 30 minutes 16-19 UTC
                for m in ((7,) if h < 16 else (7, 37)):
                    if self.r.random() < 0.03:
                        continue  # GitHub sometimes drops a scheduled run
                    events.append((utc_day + timedelta(hours=h, minutes=m + self.r.uniform(0, 10)), "tick", None))
            local_midnight = datetime(2026, 9, 7, tzinfo=TZ) + timedelta(days=d)
            events.append((local_midnight + timedelta(hours=self.r.uniform(19, 23)), "doing", None))
            if self.r.random() < 0.35:  # you file a few real tasks through an issue
                at = local_midnight + timedelta(hours=self.r.uniform(10, 23))
                events.append((at.astimezone(UTC), "inbox", None))
            if d > 2 and self.r.random() < 0.12:  # a day you work in Jira yourself
                self.human_days.add(local_midnight.date())
                for n in range(3):
                    at = local_midnight + timedelta(hours=self.r.uniform(11, 17) + n * 0.5)
                    events.append((at.astimezone(UTC), "human", None))
        return sorted(events, key=lambda e: e[0])

    def human_edit(self):
        cands = [i for i in self.jira.issues.values() if i["type"] != "Epic" and i["status"] not in self.jira.p["done"]]
        if not cands:
            return
        i = self.r.choice(cands)
        new = (self.local().date() + timedelta(days=self.r.randint(1, 6))).isoformat()
        if new == i["duedate"]:
            new = (self.local().date() + timedelta(days=7)).isoformat()
        self.jira.human(lambda: self.jira.edit(i, {"duedate": new}, "PUT", "/human"))

    def do_my_tasks(self):
        """You do your own tasks around their due date and close them in Jira."""
        today = self.local().date().isoformat()
        for k in sorted(self.real):
            i = self.jira.issues[k]
            if i["status"] not in self.jira.p["done"] and (i["duedate"] or "9999") <= today and self.r.random() < 0.6:
                done = next(s for s in self.jira.p["done"])
                self.jira.human(lambda: (self.jira._change(i, "status", i["status"], done), i.update(status=done)))

    def inbox(self):
        lines = self.r.sample(REAL_LINES, self.r.randint(1, 3))
        open_real = [k for k in self.real if self.jira.issues[k]["status"] not in self.jira.p["done"]]
        if open_real and self.r.random() < 0.4:
            k = self.r.choice(sorted(open_real))
            lines.append(f"done {k}")
            self.asked_done.add(k)
        out, _ = self.session("inbox", {"ISSUE_TITLE": "tasks", "ISSUE_BODY": "\n".join(lines)})
        for line in out.splitlines():
            if line.strip().startswith("Added "):
                self.real.add(line.split()[1])

    def run(self):
        last_day = None
        for at, what, _ in self.timeline():
            if self.clock() < at:
                self.clock.t = at
            if what == "tick":
                out, st = self.session("tick")
                if "Urgent: something came up" in out:
                    for line in out.splitlines():
                        if line.strip().startswith("+ PER-"):
                            self.urgent.add(line.split()[1])
                ld = self.local().date()
                if self.local().hour >= 6 and ld != last_day:  # first run of the morning: last night is over
                    last_day = ld
                    if ld > datetime(2026, 9, 7).date():
                        self.snapshot(ld - timedelta(days=1), st)
            elif what == "inbox":
                self.inbox()
            elif what == "human":
                self.human_edit()
            elif what == "doing":
                self.do_my_tasks()
        return self

    def snapshot(self, day, st):
        now = self.clock()
        inbox = [(now - i["created"]).total_seconds() / 86400 for i in self.jira.issues.values()
                 if i["type"] != "Epic" and i["status"] == S07]
        active = any(s["state"] == "active" for s in self.jira.sprints.values())
        meta = st.get("meta", {})
        wip = sum(1 for k, i in self.jira.issues.items() if i["status"] in (S02, "In Progress")
                  and k in meta and not meta[k].get("real") and not meta[k].get("urg"))
        self.snapshots.append((day, max(inbox or [0]), active, wip))

    # ------------------------------------------------ checks
    def checks(self):
        J, ap = self.jira, self.ap
        state = json.loads(J.props.get("/rest/api/3/user/properties/jira-autopilot-state", "{}"))
        meta = state.get("meta", {})
        issues = [i for i in J.issues.values() if i["type"] != "Epic"]
        bot_made = {k for (t, a, k, w) in J.writes if w == "create" and a == "bot"}
        tasks = [i for i in issues if i["key"] in bot_made and i["key"] not in self.real]
        sim_tasks = [i for i in tasks if i["key"] not in self.urgent]
        res, stats = [], {}

        def check(name, ok, detail):
            res.append((name, bool(ok), detail))

        # 1. your own tasks are never worked, closed, parked or re-dated for you
        bad = []
        for k in self.real:
            for (t, a, f, old, new) in J.issues[k]["history"]:
                if a != "bot":
                    continue
                if f == "status" and new in (S08, S05, S02, S03, "In Progress"):
                    bad.append(f"{k} moved to {new}")
                if f == "status" and new in J.p["done"] and k not in self.asked_done:
                    bad.append(f"{k} closed without a done line")
                if f == "duedate" and old is not None:
                    bad.append(f"{k} due date changed {old}->{new}")
            if any(a == "bot" for (t, a, s, st_) in J.issues[k]["worklogs"]):
                bad.append(f"{k} got a worklog")
        check("your own tasks untouched", not bad and self.real, f"{len(self.real)} real tasks, problems: {bad[:3]}")

        # 2. the inbox keeps up
        worst = max((s[1] for s in self.snapshots), default=0)
        if self.kind == "sepm":
            check("inbox keeps up", worst <= 4.0, f"oldest unsorted task at night: {worst:.1f} days")

        # 3. a sprint is running most of the time, and kickoff made one on day one
        after = [s for s in self.snapshots if s[0] >= self.snapshots[0][0]]
        share = sum(s[2] for s in after) / max(1, len(after))
        check("sprint always running", share >= 0.85 and after and after[0][2],
              f"{share:.0%} of nights had an active sprint, first night: {after[0][2] if after else None}")

        # 4. sprints end part-done, like real ones
        rates = [d / t for (_, d, t, _) in J.closed if t]
        later = rates[1:]  # the kickoff sprint only holds the few starter tasks, so it is always finished
        mean = sum(later) / len(later) if later else None
        too_neat = len(later) >= 3 and min(later) >= 0.98  # every sprint perfect is what looks fake
        check("sprints end part-done", later and mean >= 0.45 and not too_neat,
              f"{len(rates)} sprints closed, done share {', '.join(f'{x:.0%}' for x in rates)} (first is the kickoff)")
        stats["sprints"] = rates

        # 5. bigger tasks take days
        def cycle(i):
            hist = i["history"]
            start = next((t for (t, a, f, o, n) in hist if f == "status" and n in (S02, "In Progress")), None)
            end = next((t for (t, a, f, o, n) in hist if f == "status" and n in J.p["done"] and start and t > start), None)
            return (end - start).total_seconds() / 3600 if start and end else None
        big = [cycle(i) for i in sim_tasks if (meta.get(i["key"], {}).get("p") or 0) >= 5]
        big = [c for c in big if c is not None]
        small = [cycle(i) for i in sim_tasks if (meta.get(i["key"], {}).get("p") or 9) <= 2]
        small = [c for c in small if c is not None]
        fast_big = [c for c in big if c < 18]
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        check("big tasks take days", big and len(fast_big) <= 0.1 * len(big) and med(big) > (med(small) or 0),
              f"5-8 point tasks: median {med(big) or 0:.0f}h in progress, {len(fast_big)}/{len(big)} under 18h; "
              f"1-2 point median {med(small) or 0:.0f}h")

        # 6. no bursts: a handful of actions a minute at most, and the night spreads over several runs
        bot = sorted(t for (t, a, k, w) in J.writes if a == "bot")
        ops = sorted(set(bot))  # one API call can change several fields or issues at once
        peak, lo = 0, 0
        for hi in range(len(ops)):
            while ops[hi] - ops[lo] > timedelta(seconds=60):
                lo += 1
            peak = max(peak, hi - lo + 1)
        check("no bursts", peak <= 12, f"most actions in any 60 seconds: {peak}")  # one every 5 seconds, still human
        nights, spread = 0, 0
        for d in range(1, self.days):
            start = datetime(2026, 9, 7, 21, 30, tzinfo=TZ) + timedelta(days=d)
            ts = [t for t in bot if start <= t <= start + timedelta(hours=4)]
            if ts:
                nights += 1
                runs = 1 + sum(1 for a, b in zip(ts, ts[1:]) if (b - a) > timedelta(minutes=15))
                spread += runs >= 2
        check("night spread over runs", nights and spread / nights >= 0.7, f"{spread}/{nights} nights used 2+ runs")

        # 7. comments rarely repeat within two weeks
        said = sorted((t, txt) for i in issues for (t, a, txt) in i["comments"] if a == "bot")
        last, repeats = {}, 0
        for t, txt in said:
            if txt in last and t - last[txt] < timedelta(days=14):
                repeats += 1
            last[txt] = t
        check("comments vary", said and repeats <= 0.05 * len(said), f"{repeats} repeats in {len(said)} comments")

        # 8. a share of tasks have no due date
        def filed(i):  # when the night routine sorted it: that is when it gets assigned
            return next((t for (t, a, f, o, n) in i["history"] if f == "assignee" and a == "bot"), None)
        sorted_ = [i for i in sim_tasks if filed(i)]
        undated = [i for i in sorted_ if not any(f == "duedate" and n for (t, a, f, o, n) in i["history"]
                                                 if t <= filed(i))]
        share_nd = len(undated) / max(1, len(sorted_))
        check("some tasks undated", 0.15 <= share_nd <= 0.45,
              f"{share_nd:.0%} of {len(sorted_)} sorted tasks had no due date when sorted")

        # 9. weekends are lighter, and some days nothing happens
        made = Counter(self.local(J.issues[k]["created"]).date() for k in bot_made
                       if k not in self.urgent and k not in self.real)
        days = [datetime(2026, 9, 7).date() + timedelta(days=d) for d in range(1, self.days)]
        wk = [made[d] for d in days if d.weekday() < 5]
        we = [made[d] for d in days if d.weekday() >= 5]
        wk_avg, we_avg = sum(wk) / max(1, len(wk)), sum(we) / max(1, len(we))
        check("weekends lighter", we_avg < 0.85 * wk_avg, f"new tasks a day: weekdays {wk_avg:.1f}, weekends {we_avg:.1f}")
        active_days = {(self.local(t) - timedelta(hours=4)).date() for t in bot}  # 00:00-04:00 is last night
        quiet = [d for d in days[:-1] if d not in active_days]
        stats["quiet"] = len(quiet)
        check("quiet days rare", len(quiet) <= 0.2 * len(days), f"{len(quiet)} days with no activity: {quiet}")

        # 10. small edits happen, and typos get fixed
        edits = sum(out.count("Edit: small tidy-up") for _, _, out in self.logs)
        fixed = sum(out.count("fixed a typo") for _, _, out in self.logs)
        stale_typos = [k for k, m in meta.items() if m.get("ty") and not m.get("x") and
                       self.clock() - J.issues[k]["created"] > timedelta(days=4)]
        check("small edits happen", edits >= self.days / 7 and not stale_typos,
              f"{edits} tidy-ups, {fixed} typo fixes, typos older than 4 days: {stale_typos}")

        # 11. time gets logged, never more than a believable day
        done_after_work = [i for i in sim_tasks if cycle(i) is not None]
        logged = [i for i in done_after_work if any(a == "bot" for (t, a, s, st_) in i["worklogs"])]
        per_day = defaultdict(float)  # by the worklog's start time, the way Jira reports it
        for i in issues:
            for (t, a, s, st_) in i["worklogs"]:
                per_day[datetime.strptime(st_, "%Y-%m-%dT%H:%M:%S.000%z").astimezone(TZ).date()] += s / 3600
        check("time logged", done_after_work and len(logged) >= 0.8 * len(done_after_work)
              and max(per_day.values(), default=0) <= 9,
              f"{len(logged)}/{len(done_after_work)} finished tasks have worklogs, busiest day "
              f"{max(per_day.values(), default=0):.1f}h")

        # 12. it backs off on days you work in Jira
        def later_ops(d):  # actions from 18:00 to 03:00, after you have been in Jira that afternoon
            a = datetime.combine(d, datetime.min.time(), TZ) + timedelta(hours=18)
            return len({t for t in ops if a <= t <= a + timedelta(hours=9)})
        edits = Counter(self.local(t).date() for i in issues for (t, a, f, o, n) in i["history"] if a == "human")
        busy = {d for d in self.human_days if edits[d] >= 2}  # days you really changed 2+ things
        hd = [later_ops(d) for d in busy if d in days]
        nd = [later_ops(d) for d in days if d not in self.human_days and d not in quiet]  # noqa
        if len(hd) >= 2 and nd:  # one day is too noisy to judge
            ratio = (sum(hd) / len(hd)) / (sum(nd) / len(nd))
            check("backs off when you are active", ratio <= 0.75,
                  f"evening actions on your Jira days are {ratio:.0%} of a normal evening ({len(hd)} such days)")
        open_bot = [i for i in sim_tasks if J.cat(i["status"]) != "Done" and i["status"] != S05]
        stats["open"] = len(open_bot)
        check("backlog stays manageable", len(open_bot) <= 1.5 * self.days + 10,
              f"{len(open_bot)} of its tasks still open after {self.days} days, "
              f"{sum(1 for i in sim_tasks if i['status'] == S05)} parked for later")

        # 13. no title repeats within three weeks, and work in progress stays small
        seen, dupes = {}, []
        for i in sorted(tasks, key=lambda i: i["created"]):
            title = ap.norm(meta.get(i["key"], {}).get("ty") or i["summary"])
            if title in seen and i["created"] - seen[title] < timedelta(days=21):
                dupes.append(i["summary"])
            seen[title] = i["created"]
        check("titles do not repeat", not dupes, f"repeats: {dupes[:3]}")
        most_wip = max((s[3] for s in self.snapshots), default=0)
        check("work in progress stays small", most_wip <= self.cfg["wip_limit"] + 1, f"most at once: {most_wip}")
        stats["tasks"], stats["real"] = len(tasks), len(self.real)
        return res, stats


def simulate(seed, kind, days, verbose=False):
    sim = Sim(seed, kind, days)
    try:
        sim.run()
    except Exception:
        tail = "\n".join(out for _, _, out in sim.logs[-3:])
        return [("no crashes", False, traceback.format_exc() + "\n--- last runs ---\n" + tail[-3000:])], {}, sim
    res, stats = sim.checks()
    return [("no crashes", True, f"{len(sim.logs)} runs")] + res, stats, sim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--long", action="store_true", help="eight 90-day semesters on both kinds of project")
    ap.add_argument("--sweep", action="store_true", help="16 extra 30-day runs plus two 90-day ones")
    ap.add_argument("--seeds", help="a range like 301-310: every seed for --days days, every third one plain")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--kind", default=None, choices=["sepm", "plain"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    failed = 0
    for test in (test_parser, test_alerts, test_search_lag, test_setup):
        try:
            print(f"ok    {test()}")
        except Exception:
            failed += 1
            print(f"FAIL  {test.__name__}\n{traceback.format_exc()}")
    if args.seeds:
        a, _, b = args.seeds.partition("-")
        runs = [(n, args.kind or ("plain" if n % 3 == 0 else "sepm"), args.days) for n in range(int(a), int(b or a) + 1)]
    elif args.sweep:
        runs = [(n, "plain" if n % 4 == 0 else "sepm", 30) for n in range(5, 21)] + [(101, "sepm", 90), (102, "plain", 90)]
    elif args.seed is not None:
        runs = [(args.seed, args.kind or "sepm", args.days)]
    elif args.quick:
        runs = [(1, "sepm", 14)]
    elif args.long:
        runs = [(s, "plain" if s % 4 == 0 else "sepm", 90) for s in range(101, 109)]
    else:
        runs = [(1, "sepm", args.days), (2, "sepm", args.days), (3, "sepm", args.days), (4, "plain", args.days)]
    quiet_total = 0
    for seed, kind, days in runs:
        res, stats, sim = simulate(seed, kind, days, args.verbose)
        quiet_total += stats.get("quiet", 0)
        bad = [r for r in res if not r[1]]
        failed += len(bad)
        rates = stats.get("sprints", [])
        print(f"\n{'ok  ' if not bad else 'FAIL'}  simulation seed {seed}, {kind} project, {days} days: "
              f"{stats.get('tasks', 0)} tasks made, {stats.get('real', 0)} of yours, {stats.get('open', 0)} open at the end, "
              f"sprints {sum(rates) / max(1, len(rates)):.0%} done on average")
        for name, ok, detail in res:
            if args.verbose or not ok:
                print(f"   {'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        if args.verbose:
            for _, cmd, out in sim.logs[-6:]:
                print(out)
    if len(runs) >= 4 and quiet_total == 0:  # 8% of days, so across 4+ runs at least one should appear
        failed += 1
        print("FAIL  quiet days never happened across all simulations")
    print(f"\n{'All tests passed.' if not failed else f'{failed} check(s) failed.'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
