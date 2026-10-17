#!/usr/bin/env python3
"""
Jira Autopilot
Keeps a Jira project active on a rhythm that looks like a real person:
  - drops new tasks into the inbox at random gaps, fewer and lighter at weekends
  - morning: reviews deferred items and starts work, never more than a couple at once
  - night, over three runs about half an hour apart: sorts the inbox, works through
    tasks in progress (bigger tasks take days and log time), then sprint upkeep
  - every week: closes the sprint on its end date and plans the next one
  - some days: an urgent task comes up mid-day and gets done within hours
  - some days: nothing happens at all, and it backs off on days you use Jira yourself
  - your own tasks arrive through GitHub issues and are never closed for you
Needs only Python 3.9+. Uses no AI tokens day to day.

Commands:
  python autopilot.py check     verify credentials and show how your project maps
  python autopilot.py audit     compact summary of your project
  python autopilot.py tick      what the scheduler runs; does whatever is due right now
                                (the very first tick runs the kickoff: a few tasks and a starter sprint)
  python autopilot.py kickoff   run that first-run setup now
  python autopilot.py urgent    something comes up: add an urgent task and start it now
  python autopilot.py inbox     add your own tasks from ISSUE_TITLE / ISSUE_BODY (the issue workflow)
  python autopilot.py setup     write tasks.json from ISSUE_BODY with Gemini (the issue workflow)
  python autopilot.py drop | morning | night | sprint | edit    force one phase now
  python autopilot.py refill    ask Gemini (optional, GEMINI_API_KEY) for fresh task ideas
Add --dry-run to any command to see what would happen without changing Jira.
"""
import argparse
import base64
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PROP = "jira-autopilot-state"
STOP = {"and", "the", "of", "for", "a", "to", "my", "with", "in", "on", "at"}
PLAN_WORDS = ("meet", "meeting", "call", "appointment", "office hours", "quiz", "exam",
              "presentation", "match", "session", "dinner", "wedding", "visit", "interview", "viva")

DEFAULTS = {
    "project_key": "",
    "issue_type": "Task",
    "utc_offset_hours": 5,
    "active_hours": [9, 22],
    "drop_gap_hours": [3.0, 4.5],
    "weekend_drop_gap_hours": [4.0, 6.0],
    "drop_count_weights": [75, 20, 5],
    "quick_ratio": 0.4,
    "everyday_ratio": 0.55,
    "weekend_everyday_ratio": 0.8,
    "no_due_ratio": 0.3,
    "morning_window": ["08:00", "09:30"],
    "weekend_morning_window": ["09:30", "11:30"],
    "night_window": ["21:45", "22:50"],
    "weekend_days": [5, 6],
    "days_off": [],
    "quiet_day_chance": 0.08,
    "lazy_night_chance": 0.2,
    "daily_hours": [3.0, 5.0],
    "weekend_hours": [1.0, 3.5],
    "wip_limit": 2,
    "log_work": True,
    "edit_chance": 0.45,
    "typo_chance": 0.06,
    "back_off": True,
    "action_gap_seconds": [10, 30],
    "sort_limit": 10,
    "click_seconds": [2, 6],
    "sprint_every_days": 2,
    "sprint_length_days": 7,
    "sprint_size": [6, 12],
    "sprint_name_prefix": "",
    "kickoff_tasks": [4, 6],
    "gemini_model": "gemini-3.8-flash",
    "context_file": "context.md",
    "urgent_day_chance": 0.25,
    "urgent_work_hours": [1.0, 4.0],
    "urgent_slip_chance": 0.15,
    "jitter_minutes": 0,
    "board_id": None,
    "touch_existing_tasks": False,
    "label": "",
    "allow_create_epics": True,
    "statuses": {
        "inbox": ["07 - BACKLOG UNPROCESSED", "Inbox"],
        "deferred": ["08 - DEFER BACKLOG UNPROCESSED", "Deferred"],
        "process_today": ["06 - BACKLOG PROCESS TODAY"],
        "backlog": ["04 - BACKLOG THIS SEMESTER", "To Do", "Backlog", "Open"],
        "later": ["05 - BACKLOG NOT THIS SEMESTER", "Later", "Someday"],
        "plan": ["09 - PLAN", "Planned", "Selected for Development"],
        "in_progress": ["02 - IN PROGRESS", "In Progress"],
        "review": ["03 - UNDER REVIEW", "In Review", "Review"],
        "done": ["01 - DONE", "Done", "Closed", "Resolved"],
    },
    "fields": {
        "story_points": ["Story Points", "Story point estimate"],
        "defer_until": ["Defer Until"],
        "deep_shallow": ["Deep/Shallow?", "Deep/Shallow"],
        "task_size": ["Task Size"],
    },
    "priorities": {
        "urgent": ["TODAY", "High", "Highest"],
        "normal": ["NOT TODAY", "Medium", "Low"],
    },
    "area_epics": {
        "chores": "Household", "errands": "Errands", "tech": "Devices & Tech",
        "social": "Social", "family": "Family", "health": "Body upkeep",
        "self": "Self improvement", "admin": "Documents", "finance": "Money",
        "career": "Job search",
    },
    "area_components": {
        "course": ["Coursework", "Studies", "University"],
        "project": ["Startup & Side Projects", "Robocat (own project)", "Projects"],
        "chores": ["Admin & Errands", "Home", "Personal"],
        "errands": ["Admin & Errands", "Errands", "Personal"],
        "tech": ["Devices & Tech", "Tech"],
        "social": ["Social", "Personal"],
        "family": ["Family", "Personal"],
        "health": ["Health", "Personal"],
        "self": ["Career", "Personal", "Learning"],
        "admin": ["Admin & Errands", "Admin"],
        "finance": ["Admin & Errands", "Finance"],
        "career": ["Career"],
    },
}

# {topic} is the course or project short name; those lines are only used when there is one.
COMMENTS = {
    "start": ["Started on this.", "Got going on this before class.", "Picked this up today.",
              "Starting now, should not take long.", "Made a start, more tomorrow.", "On it.",
              "Opened this up, working out where to begin.", "Starting with the easy part.",
              "Finally starting this one.", "Blocked out some time for this today.",
              "Starting after lunch.", "First pass today.", "Reading up before I start properly.",
              "Making a start while I have the energy.", "Doing this one first today.",
              "Starting this in the library.", "Gave myself an hour for this.", "Getting this going tonight.",
              "Started, will see how far I get.", "Kicking this off now.", "Starting before I lose motivation.",
              "Set up everything I need, starting.", "Getting the {topic} part out of the way first.",
              "Started the {topic} work for this.", "Back on {topic} stuff, starting this."],
    "progress": ["About halfway there.", "Decent progress, finishing tomorrow.", "Got stuck for a bit, back on track now.",
                 "Most of it is done.", "Slow going but moving.", "Did about an hour on this.",
                 "Took longer than I thought, still going.", "Main part done, cleanup left.",
                 "One more session should do it.", "Hit a snag, will look again tomorrow.",
                 "Good chunk done tonight.", "Almost there.", "Went back and redid part of it.",
                 "Picked this back up after a break.", "Rough version done, polishing next.",
                 "Done the boring part at least.", "Further along than I expected.", "Still on it, no rush.",
                 "Had to look a few things up, moving again.", "Two thirds done.", "Only small bits left.",
                 "The {topic} part is mostly there.", "The {topic} bit is trickier than expected.",
                 "Worked on the {topic} side of this tonight."],
    "done": ["Done.", "Finished this one.", "Wrapped up.", "Sorted.", "Done, took longer than expected.",
             "Finally done.", "Finished, quicker than I thought.", "All done here.", "Closed it out.",
             "Done and checked.", "That's done.", "Got it done tonight.", "Done, moving on.",
             "Done, one less thing.", "Ticked off.", "Finished it in one go.", "Done before the deadline.",
             "Out of the way now.", "Completed, nothing left on this.", "Finished, happy with it.",
             "Done, that was overdue.", "Got through it.", "Finished the {topic} part, all good.",
             "{topic} side of this is done."],
    "push": ["Ran out of time, doing this tomorrow.", "Pushing this a couple of days.", "Could not get to it today.",
             "Busy day, moving this.", "Will pick this up after the weekend.", "Something else came up, moving this.",
             "Need to wait on something before I can do this.", "Moving this back a bit.",
             "Too tired tonight, tomorrow.", "Did not happen today.", "Rescheduling this one.",
             "Classes ran late, moving this.", "Not urgent, pushing it.", "Will do this later in the week.",
             "Swapped this with something more pressing.", "Need a free evening for this, moving it.",
             "Postponing, not enough time today.", "Bumping this to next week.",
             "Not today, too much {topic} work.", "{topic} deadlines first, this can wait."],
    "review": ["Done, just want to double check it.", "Finished, one more look before closing.",
               "Mostly done, reviewing tomorrow.", "Want to go over this once more.",
               "Done, checking it with fresh eyes later.", "Finished the draft, review pending.",
               "Need to proofread before closing.", "Ready, giving it a final check.",
               "Done, will skim it once in the morning.", "Finished, checking nothing is missing.",
               "Complete, quick review left.", "Draft done, rereading tomorrow."],
    "reopen": ["Found a problem, reopening.", "Not quite done, missed something.", "Went back to fix one thing.",
               "Reopening, needs another pass.", "Spotted a mistake, fixing it.", "One part was wrong, redoing it.",
               "Forgot a step, back on it."],
    "urgent": ["This just came up, doing it now.", "Dropping everything else for this one.",
               "Has to be done today, on it.", "Last minute, starting right away.",
               "Need this done by tonight, starting now.", "Bumping this to the top.", "Sorting this right away.",
               "Did not see this coming, doing it now.", "Today or never, starting.", "Everything else can wait."],
}
DESC = {
    "q": ["Small one, just get it done.", "Should take five minutes.", "Keep forgetting this one.",
          "Do it between classes.", "Quick task, do not overthink it.", "Needs doing soon.",
          "Two minute job.", "Do it on the way back.", "Easy one."],
    "b": ["Needs a proper sit-down session.", "Block an hour or two for this.", "Break it into parts if it drags.",
          "Want this done before the weekend.", "Been putting this off, time to start.", "Do this on a free evening.",
          "Needs focus, no phone.", "Start early, this will take a while.", "Do the hard part first."],
    "u": ["Came up today, needs doing before tonight.", "Just found out about this, it is due today.",
          "Urgent, push everything else back.", "Has to be sorted today."],
}
HOURS = {1: 0.5, 2: 1.0, 3: 2.0, 5: 4.0, 8: 6.5}  # rough hours of work per story point size

rng = random.Random(os.environ.get("AUTOPILOT_SEED"))
CLOCK = None  # tests swap in a fake clock: a function returning an aware UTC datetime
SLEEP = time.sleep  # tests swap in a function that moves the fake clock instead


# ---------------------------------------------------------------- helpers

def log(msg=""):
    print(msg, flush=True)


def utcnow():
    return CLOCK() if CLOCK else datetime.now(timezone.utc)


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def tokens(s):
    return {t for t in norm(s).split() if t not in STOP}


def adf(text):
    paras = [p for p in str(text).split("\n") if p.strip()] or [" "]
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras]}


def jq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_dt(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_day(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def weighted(choices, weights):
    return rng.choices(choices, weights=weights, k=1)[0]


def rand_time(window):
    a, b = hhmm(window[0]), hhmm(window[1])
    m = rng.randint(a, max(a, b))
    return f"{m // 60:02d}:{m % 60:02d}"


def typo(title):
    """A believable slip: two neighbouring letters swapped in one longer word."""
    words = title.split()
    longer = [n for n, w in enumerate(words) if len(w) >= 6 and w.isalpha()]
    if not longer:
        return None
    n = rng.choice(longer)
    w = words[n]
    p = rng.randint(1, len(w) - 3)
    words[n] = w[:p] + w[p + 1] + w[p] + w[p + 2:]
    out = " ".join(words)
    return out if out != title else None


def day_off(cfg, day):
    """days_off holds dates like "2026-12-25" or ranges like "2026-12-20..2027-01-02"."""
    iso = day.isoformat()
    for d in cfg.get("days_off") or []:
        a, _, b = str(d).partition("..")
        if a <= iso <= (b or a):
            return True
    return False


# ---------------------------------------------------------------- Jira client

class JiraError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Jira:
    def __init__(self, site, email, token, dry=False):
        base = site.strip().rstrip("/")
        if not base.startswith("http"):
            base = "https://" + base
        self.base = base
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}", "Accept": "application/json",
                        "Content-Type": "application/json"}
        self.dry = dry

    def req(self, method, path, body=None, params=None, write=False):
        if write and self.dry:
            preview = json.dumps(body)[:160] if body is not None else ""
            log(f"    [dry-run] {method} {path} {preview}")
            return {}
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            request = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=40) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(3 * (attempt + 1))
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise JiraError(e.code, f"{method} {path} -> {e.code} {detail}")
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise JiraError(0, f"{method} {path} -> {e}")
                time.sleep(3)
        raise JiraError(0, f"{method} {path} -> gave up after retries")

    def get(self, path, **params):
        return self.req("GET", path, params=params or None)

    def post(self, path, body):
        return self.req("POST", path, body, write=True)

    def put(self, path, body):
        return self.req("PUT", path, body, write=True)

    def search(self, jql, fields, limit=100):
        out, token = [], None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": min(100, limit - len(out))}
            if token:
                body["nextPageToken"] = token
            r = self.req("POST", "/rest/api/3/search/jql", body)
            out += r.get("issues", [])
            token = r.get("nextPageToken")
            if not token or r.get("isLast") or len(out) >= limit:
                return out


# ---------------------------------------------------------------- state (stored in Jira, local file fallback)

class Store:
    def __init__(self, jira, account_id, dry, path=os.path.join(HERE, "state.json")):
        self.j, self.acc, self.dry, self.path = jira, account_id, dry, path
        self.where = "jira"

    def load(self):
        try:
            r = self.j.get(f"/rest/api/3/user/properties/{STATE_PROP}", accountId=self.acc)
            return r.get("value") or {}
        except JiraError as e:
            if e.code == 404:
                return load_json(self.path, {}) if self.path else {}
            self.where = "file"
            log(f"  note: could not read state from Jira ({e.code}), using state.json")
            return load_json(self.path, {}) if self.path else {}

    def save(self, st):
        meta = st.get("meta", {})
        # Jira caps a user property at 32 KB: forget finished tasks first, oldest first
        while len(json.dumps(st)) > 30000 and meta:
            old = next((k for k, v in meta.items() if v.get("x")), None) or next(iter(meta))
            meta.pop(old)
        if self.dry:
            return
        if self.path:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=1)
        if self.where == "jira":
            try:
                self.j.req("PUT", f"/rest/api/3/user/properties/{STATE_PROP}", st,
                           params={"accountId": self.acc})
            except JiraError as e:
                log(f"  note: could not save state to Jira ({e.code}), kept state.json only")


# ---------------------------------------------------------------- task pool

def mk_task(t):
    if isinstance(t, dict):
        return {"t": t.get("t") or t.get("title"), "s": (t.get("s") or t.get("size") or "b")[0].lower(),
                "a": t.get("a") or t.get("area") or "self", "d": t.get("d") or t.get("note") or ""}
    t = list(t) + ["", "", ""]
    return {"t": t[0], "s": (t[1] or "b")[0].lower(), "a": t[2] or "self", "d": t[3] or ""}


class Pool:
    def __init__(self, generic, personal, extra):
        self.personal = personal
        self.courses = [c if isinstance(c, dict) else {"name": c} for c in personal.get("courses", [])]
        self.projects = [p if isinstance(p, dict) else {"name": p} for p in personal.get("projects", [])]
        self.everyday = [mk_task(t) for t in generic.get("everyday", [])]
        self.specific = [mk_task(t) for t in personal.get("tasks", [])] + [mk_task(t) for t in extra]
        self.templated, self.urgent_mine = [], [mk_task(t) for t in personal.get("urgent", [])]
        self.urgent_everyday = [mk_task(t) for t in generic.get("urgent", [])]
        for c in self.courses:
            short = c.get("short") or c["name"]
            for tpl in generic.get("course_templates", []):
                self.templated.append({"t": tpl[0].format(c=short), "s": tpl[1], "a": c["name"], "d": ""})
            for tpl in generic.get("urgent_course_templates", []):
                self.urgent_mine.append({"t": tpl[0].format(c=short), "s": tpl[1], "a": c["name"], "d": ""})
        for p in self.projects:
            short = p.get("short") or p["name"]
            for tpl in generic.get("project_templates", []):
                self.templated.append({"t": tpl[0].format(p=short), "s": tpl[1], "a": p["name"], "d": ""})
            for tpl in generic.get("urgent_project_templates", []):
                self.urgent_mine.append({"t": tpl[0].format(p=short), "s": tpl[1], "a": p["name"], "d": ""})

    def kind(self, area):
        n = norm(area)  # a course or project can be named in full or by its short name
        if any(n in (norm(c["name"]), norm(c.get("short"))) for c in self.courses):
            return "course"
        if any(n in (norm(p["name"]), norm(p.get("short"))) for p in self.projects):
            return "project"
        return n if n else "self"

    def short(self, area):
        n = norm(area)
        for c in self.courses + self.projects:
            if n in (norm(c["name"]), norm(c.get("short"))):
                return c.get("short") or c["name"]
        return None

    def fresh(self, bucket, used):
        return [t for t in bucket if t["t"] and norm(t["t"]) not in used]

    def pick_urgent(self, used):
        mine, everyday = self.fresh(self.urgent_mine, used), self.fresh(self.urgent_everyday, used)
        bucket = mine if mine and (rng.random() < 0.6 or not everyday) else everyday
        return rng.choice(bucket) if bucket else None

    def pick(self, n, used, everyday_ratio, quick_ratio):
        out, used = [], set(used)
        for _ in range(n):
            if rng.random() < everyday_ratio:
                order = [self.everyday, self.specific, self.templated]
            elif rng.random() < 0.6:
                order = [self.specific, self.templated, self.everyday]
            else:
                order = [self.templated, self.specific, self.everyday]
            size = "q" if rng.random() < quick_ratio else "b"
            choice = None
            for bucket in order:
                cands = self.fresh(bucket, used)
                sized = [t for t in cands if t["s"] == size]
                if sized or cands:
                    choice = rng.choice(sized or cands)
                    break
            if not choice:
                break
            used.add(norm(choice["t"]))
            out.append(choice)
        return out


# ---------------------------------------------------------------- Gemini (optional)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def gemini_key():
    return os.environ.get("GEMINI_API_KEY", "").strip()


def llm_json(prompt, model, shape="["):
    """One Gemini call. Returns the JSON array (or object, with shape "{") in the reply, or None.
    Temperature stays at the default, because Google warns lower values make Gemini 3 loop."""
    key = gemini_key()
    if not key:
        return None
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    request = urllib.request.Request(GEMINI_URL, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
        m = re.search(r"\[[\s\S]*\]" if shape == "[" else r"\{[\s\S]*\}", content)
        return json.loads(m.group(0)) if m else None
    except urllib.error.HTTPError as e:  # never let the optional AI break the run
        log(f"  Gemini call failed: {e.code} {e.read().decode(errors='replace')[:200]}")
        return None
    except Exception as e:
        log(f"  Gemini call failed: {e}")
        return None


def llm_array(prompt, model):
    return llm_json(prompt, model, "[")


CONTEXT_LIMIT = 8000  # characters of context.md sent to Gemini


def load_context(path):
    """context.md: a short, regularly refreshed description of you and your current work."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()[:CONTEXT_LIMIT]
    except FileNotFoundError:
        return ""


def about(personal):
    if personal.get("context"):
        return "About this person, kept up to date every week:\n" + personal["context"] + "\n"
    return f"Person: {personal.get('profile', 'a university student')}\n"


def llm_tasks(personal, recent_titles, model, n=25):
    courses = ", ".join((c if isinstance(c, str) else c.get("name", "")) for c in personal.get("courses", []))
    projects = ", ".join((p if isinstance(p, str) else p.get("name", "")) for p in personal.get("projects", []))
    focus = ("Base most course and project tasks on what they are working on right now and what is coming up. "
             "Name the real assignment, feature, bug or deadline instead of something generic.\n"
             if personal.get("context") else "")
    prompt = (
        f"Generate {n} new to-do items for this person's personal Jira board.\n"
        + about(personal) +
        f"Courses: {courses or 'none'}\nProjects: {projects or 'none'}\n"
        f"Avoid these recent titles: {'; '.join(list(recent_titles)[:40])}\n"
        "Mix about half everyday life tasks and half course or project tasks. About 40% quick (under 10 min).\n"
        + focus +
        'Return ONLY a JSON array of [title, "q" or "b", area] where area is a course name, a project name, '
        "or one of chores, errands, tech, social, family, health, self, admin, finance. Titles 3 to 9 words."
    )
    items = llm_array(prompt, model) or []
    return [mk_task(t) for t in items if t and (isinstance(t, (list, dict)))]


def llm_comments(personal, model):
    prompt = (
        "Write 40 short comments a university student leaves on their own Jira tasks.\n"
        + about(personal) +
        "Casual and specific, 3 to 12 words, no emojis, no hashtags, no names of tasks.\n"
        "About 8 of each kind: start, progress, done, push (postponing), review (finished, checking once more).\n"
        "Return ONLY a JSON array of [kind, comment]."
    )
    items = llm_array(prompt, model) or []
    return [[k, re.sub(r"[{}]", "", str(c)).strip()] for k, c in
            (x for x in items if isinstance(x, list) and len(x) == 2)
            if k in ("start", "progress", "done", "push", "review") and 3 <= len(str(c)) <= 90]


# ---------------------------------------------------------------- project context

class Ctx:
    def __init__(self, cfg, jira, st, generic, personal):
        self.cfg, self.j, self.st = cfg, jira, st
        self.personal = personal
        self.key = cfg["project_key"]
        self.tz = timezone(timedelta(hours=float(cfg["utc_offset_hours"])))
        me = jira.get("/rest/api/3/myself")
        self.me, self.me_name = me["accountId"], me.get("displayName", "")
        self.project_name = jira.get(f"/rest/api/3/project/{self.key}").get("name", self.key)

        types = jira.get(f"/rest/api/3/project/{self.key}/statuses")
        itype = next((t for t in types if norm(t["name"]) == norm(cfg["issue_type"])), None)
        if not itype:
            itype = next((t for t in types if not t.get("subtask") and norm(t["name"]) != "epic"),
                         types[0] if types else {"name": cfg["issue_type"], "statuses": []})
        self.issue_type = itype["name"]
        names = [s["name"] for s in itype.get("statuses", [])]
        self.status_names = names
        self.status = {role: self._match(names, cands) for role, cands in cfg["statuses"].items()}
        for role, hint in (("backlog", "to do"), ("in_progress", "progress"), ("done", "done")):
            if not self.status.get(role):
                self.status[role] = next((n for n in names if hint in n.lower()), None)
        if not self.status.get("backlog") and names:
            self.status["backlog"] = names[0]

        fields = jira.get("/rest/api/3/field")
        self.f = {}
        for k, cands in cfg["fields"].items():
            self.f[k] = None
            for c in cands:
                hit = next((x["id"] for x in fields if norm(x["name"]) == norm(c)), None) or \
                      next((x["id"] for x in fields if norm(x["name"]).startswith(norm(c))), None)
                if hit:
                    self.f[k] = hit
                    break
        self.components = [c["name"] for c in jira.get(f"/rest/api/3/project/{self.key}/components")]
        self.epics = [{"key": i["key"], "summary": i["fields"]["summary"]}
                      for i in jira.search(f"project = {jq(self.key)} AND issuetype = Epic", ["summary"], 200)]
        self.pool = Pool(generic, personal, st.get("extra", []))
        self._meta = None
        self.epic_budget = 1 if cfg["allow_create_epics"] else 0
        self.unsettable = set(st.get("unsettable", []))
        self.epic_needs_component = st.get("epic_needs_component", False)

    # time
    def now(self):
        return utcnow().astimezone(self.tz)

    def today(self):
        return self.now().date()

    def day(self, plus=0):
        return (self.today() + timedelta(days=plus)).isoformat()

    @staticmethod
    def _match(names, cands):
        nn = {norm(n): n for n in names}
        for c in cands:
            if norm(c) in nn:
                return nn[norm(c)]
        return None

    # jql
    def sjql(self, *roles):
        names = [self.status[r] for r in roles if self.status.get(r)]
        return f"status in ({', '.join(jq(n) for n in names)})" if names else None

    def base(self):
        return f"project = {jq(self.key)} AND issuetype != Epic"

    def search_fields(self):
        return ["summary", "status", "duedate", "priority", "created", "updated", "parent", "components"] + \
               [v for v in self.f.values() if v]

    def find(self, extra, order="ORDER BY duedate ASC", limit=200):
        issues = self.j.search(f"{self.base()} AND {extra} {order}", self.search_fields(), limit)
        if self.cfg["touch_existing_tasks"]:
            return issues
        mine = self.st.get("meta", {})
        return [i for i in issues if i["key"] in mine]

    def meta(self, key):
        return self.st.setdefault("meta", {}).setdefault(key, {})

    def touch(self, key):
        """Remember when the autopilot last changed an issue, so your own edits can be told apart.
        Called after every change, so it also leaves a few seconds before the next click."""
        m = self.st.get("meta", {}).get(key)
        if m is None and self.cfg["touch_existing_tasks"]:  # your older tasks too, once it changes them
            m = self.meta(key)
        if m is not None:
            m["bt"] = round(utcnow().timestamp())
        if not self.j.dry:
            SLEEP(rng.uniform(*self.cfg["click_seconds"]))

    def is_role(self, issue, role):
        name = self.status.get(role)
        return bool(name) and norm(issue["fields"]["status"]["name"]) == norm(name)

    # writes
    def move(self, key, role):
        target = self.status.get(role)
        if not target:
            return False
        trs = self.j.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        for t in trs:
            if norm(t["to"]["name"]) == norm(target):
                try:
                    self.j.post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                except JiraError as e:
                    if e.code != 400:
                        raise
                    log(f"  {key}: Jira refused the move to {target} ({e}), skipped")
                    return False
                self.touch(key)
                log(f"  {key} -> {target}")
                return True
        log(f"  {key}: no direct transition to {target}, skipped")
        return False

    def finish(self, key):
        ok = self.move(key, "done") or (self.move(key, "in_progress") and self.move(key, "done"))
        if ok:
            m = self.st.get("meta", {}).get(key)
            if m is not None:
                for k in [k for k in m if k not in ("a", "s", "p", "real", "ty")]:
                    m.pop(k)
                m["x"] = 1
        return ok

    def edit(self, key, fields):
        fields = {k: v for k, v in fields.items() if v is not None and k not in self.unsettable}
        if not fields:
            return
        try:
            self.j.put(f"/rest/api/3/issue/{key}", {"fields": fields})
            self.touch(key)
        except JiraError as e:
            if e.code != 400:
                log(f"  {key}: could not set {', '.join(fields)} ({e.code})")
                return
            for k, v in fields.items():
                try:
                    self.j.put(f"/rest/api/3/issue/{key}", {"fields": {k: v}})
                    self.touch(key)
                except JiraError as e2:
                    if "cannot be set" in str(e2) or "not on the appropriate screen" in str(e2):
                        self.unsettable.add(k)
                        self.st["unsettable"] = sorted(self.unsettable)
                        log(f"  {k} is not on your issue screen, leaving it out from now on")
                    else:
                        log(f"  {key}: skipped field {k}")

    def topic(self, key):
        return self.pool.short(self.st.get("meta", {}).get(key, {}).get("a", ""))

    def pick_comment(self, kind, topic=None):
        """A phrase not used on this board in the last two weeks."""
        said = self.st.setdefault("said", {})
        cutoff = self.day(-14)
        for t in [t for t, d in said.items() if d < cutoff]:
            said.pop(t)
        pool = COMMENTS[kind] + [t for k, t in self.st.get("bank", []) if k == kind]
        pool = [c for c in pool if "{topic}" not in c or topic]
        fresh = [c for c in pool if c.format(topic=topic) not in said] or pool
        text = rng.choice(fresh).format(topic=topic)
        said[text] = self.day()
        return text

    def comment(self, key, kind):
        text = self.pick_comment(kind, self.topic(key))
        self.j.post(f"/rest/api/3/issue/{key}/comment", {"body": adf(text)})
        self.touch(key)
        log(f"  {key}: commented '{text}'")

    def log_work(self, key, hours):
        if hours < 0.1 or not self.cfg["log_work"] or self.st.get("no_worklog"):
            return
        secs = max(900, int(round(hours * 4) / 4 * 3600))
        started = self.now() - timedelta(seconds=secs + rng.randint(0, 1800))
        try:
            self.j.post(f"/rest/api/3/issue/{key}/worklog",
                        {"timeSpentSeconds": secs, "started": started.strftime("%Y-%m-%dT%H:%M:%S.000%z")})
            self.touch(key)
        except JiraError as e:
            if e.code not in (400, 403):
                raise
            self.st["no_worklog"] = True
            log("  time tracking is off in this project, not logging work from now on")

    def add_to_sprint(self, sprint_id, keys):
        if not keys:
            return
        self.j.post(f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": keys})
        for k in keys:
            self.touch(k)

    # metadata
    def meta_fields(self, key):
        if self._meta is None:
            try:
                self._meta = self.j.get(f"/rest/api/3/issue/{key}/editmeta").get("fields", {})
            except JiraError:
                self._meta = {}
        return self._meta

    def option(self, key, field_key, keywords):
        fid = self.f.get(field_key)
        if not fid:
            return None
        vals = self.meta_fields(key).get(fid, {}).get("allowedValues") or []
        for kw in keywords:
            for v in vals:
                if kw in str(v.get("value") or v.get("name") or "").lower():
                    return {"id": v["id"]}
        return None

    def priority(self, key, urgent):
        vals = self.meta_fields(key).get("priority", {}).get("allowedValues") or []
        by_name = {norm(v.get("name")): v for v in vals}
        for c in self.cfg["priorities"]["urgent" if urgent else "normal"]:
            if norm(c) in by_name:
                return {"id": by_name[norm(c)]["id"]}
        return None

    def is_urgent(self, issue):
        p = norm((issue["fields"].get("priority") or {}).get("name"))
        return p in {norm(x) for x in self.cfg["priorities"]["urgent"]}

    def epic_for(self, area):
        """Best matching epic. Projects that use epics always get one, because some workflows
        refuse to move a task without it: a close match, a new epic, else the closest one or 'General'."""
        kind = self.pool.kind(area)
        name = self.cfg["area_epics"].get(kind) if kind not in ("course", "project") else area
        want = tokens(name or area)
        best, score = None, 0.0
        for e in self.epics:
            have = tokens(e["summary"])
            if want and have:
                s = len(want & have) / min(len(want), len(have))
                if s > score:
                    best, score = e, s
        if best and score >= 0.5:
            return best["key"]
        if name and self.epic_budget > 0:
            self.epic_budget -= 1
            key = self.create_epic(name, area)
            if key:
                return key
        if not self.epics:
            return None  # project does not use epics
        if best:
            return best["key"]
        general = next((e for e in self.epics if norm(e["summary"]) == "general"), None)
        if general:
            return general["key"]
        return self.create_epic("General", area) if self.cfg["allow_create_epics"] else None

    def create_epic(self, name, area):
        fields = {"project": {"key": self.key}, "issuetype": {"name": "Epic"}, "summary": name}
        if self.epic_needs_component:
            fields["components"] = self.component_for(area)
        try:
            r = self.j.post("/rest/api/3/issue", {"fields": fields})
        except JiraError as e:
            if e.code != 400 or "omponent" not in str(e) or "components" in fields:
                log(f"  could not create epic '{name}' ({e.code})")
                return None
            self.epic_needs_component = self.st["epic_needs_component"] = True  # some projects require one
            fields["components"] = self.component_for(area)
            try:
                r = self.j.post("/rest/api/3/issue", {"fields": fields})
            except JiraError as e2:
                log(f"  could not create epic '{name}' ({e2.code})")
                return None
        log(f"  created epic '{name}' {r.get('key', '')}")
        if r.get("key"):
            self.epics.append({"key": r["key"], "summary": name})
        return r.get("key")

    def component_for(self, area):
        """Matching component, else the project's first one, since some workflows require one."""
        kind = self.pool.kind(area)
        have = {norm(c): c for c in self.components}
        for c in self.cfg["area_components"].get(kind, []):
            if norm(c) in have:
                return [{"name": have[norm(c)]}]
        return [{"name": self.components[0]}] if self.components else None

    def info(self, issue):
        m = self.st.get("meta", {}).get(issue["key"])
        if m and "a" in m and "s" in m:
            return m
        f = issue.get("fields") or {}
        sp = f.get(self.f["story_points"]) if self.f.get("story_points") else None
        size = "q" if (isinstance(sp, (int, float)) and sp <= 1) else "b"
        guess = {"a": self.guess_area(f.get("summary")), "s": size, "d": None}
        if m is not None:  # an older task it has only touched: keep what it knows, fill the rest
            for k, v in guess.items():
                m.setdefault(k, v)
            return m
        return guess

    def guess_area(self, title):
        words = set(norm(title).split())
        for c in self.pool.courses + self.pool.projects:
            if tokens(c.get("short") or c["name"]) & words:
                return c["name"]
        if words & {"quiz", "assignment", "lab", "lecture", "exam", "class"}:
            return "course" if not self.pool.courses else self.pool.courses[0]["name"]
        for e in self.epics:  # a word from one of your epics, like "DBMS" or "Robocat"
            if tokens(e["summary"]) & words:
                return e["summary"]
        return "self"

    def is_real(self, key):
        """Tasks you added yourself through the inbox: sorted and planned, never worked or closed for you."""
        return bool(self.st.get("meta", {}).get(key, {}).get("real"))

    def remember(self, key, data):
        self.st.setdefault("meta", {})[key] = data


# ---------------------------------------------------------------- pacing and effort

def pause(ctx, lo, hi):
    """Wait a little between actions so the Jira history does not all share one timestamp."""
    if not ctx.j.dry:
        SLEEP(rng.uniform(lo, hi))


def breathe(ctx):
    pause(ctx, *ctx.cfg["action_gap_seconds"])


def effort_hours(ctx, issue):
    """Hours of work a task takes, from its size and story points, give or take."""
    m = ctx.st.get("meta", {}).get(issue["key"], {})
    if m.get("s") == "q":
        return round(rng.uniform(0.2, 0.6), 2)
    pts = m.get("p")
    if not pts and ctx.f.get("story_points"):
        pts = issue["fields"].get(ctx.f["story_points"])
    base = HOURS.get(int(pts)) if pts and int(pts) in HOURS else (float(pts) * 0.7 if pts else 2.0)
    return round(base * rng.uniform(0.7, 1.4), 2)


def start_work(ctx, issue, say=0.25):
    k = issue["key"]
    if not ctx.move(k, "in_progress"):
        return False
    m = ctx.meta(k)
    m.setdefault("h", effort_hours(ctx, issue))
    m["w0"] = ctx.day()
    if rng.random() < say:
        ctx.comment(k, "start")
    return True


def human_active(ctx):
    """True when someone other than the autopilot has changed tasks in Jira today."""
    if not ctx.cfg["back_off"]:
        return False
    if ctx.st.get("human_day") == ctx.day():
        return True
    meta = ctx.st.get("meta", {})
    n = 0
    for i in ctx.j.search(f"{ctx.base()} AND updated >= startOfDay()", ["updated"], 100):
        up = parse_dt(i["fields"].get("updated"))
        bt = meta.get(i["key"], {}).get("bt")
        if i["key"] not in meta or (up and bt and up.timestamp() > bt + 120):
            n += 1
    if n >= 3:
        ctx.st["human_day"] = ctx.day()
        log(f"  you changed {n} tasks in Jira today, keeping it light")
        return True
    return False


# ---------------------------------------------------------------- phases

def phase_drop(ctx, count=None, pace=None):
    log("Drop: adding inbox items")
    cfg, st = ctx.cfg, ctx.st
    recent = ctx.j.search(f"project = {jq(ctx.key)} AND created >= -21d", ["summary"], 300)
    used = {norm(i["fields"]["summary"]) for i in recent}
    used |= {norm(m["ty"]) for m in st.get("meta", {}).values() if m.get("ty")}
    ts = utcnow().timestamp()
    context = ctx.personal.get("context", "")
    ctx_hash = format(sum(ord(c) * (n + 1) for n, c in enumerate(context)) % 1000003, "x") if context else None
    changed = ctx_hash and ctx_hash != st.get("ctx_hash") and ts >= st.get("ctx_after", 0)
    low = len(ctx.pool.fresh(ctx.pool.specific, used)) < 10 and ts >= st.get("llm_after", 0)
    if gemini_key() and (changed or low):
        st["llm_after"] = ts + 3 * 86400  # at most one idea refill every three days
        st["ctx_after"] = ts + 86400       # and at most one a day when your context changes
        extra = llm_tasks(ctx.personal, [i["fields"]["summary"] for i in recent], cfg["gemini_model"])
        if extra:
            new = [[t["t"], t["s"], t["a"]] for t in extra]
            # fresh ideas from a new context go first, older ones fade out
            st["extra"] = (new + st.get("extra", [])[:30]) if changed else (st.get("extra", []) + new)[-80:]
            ctx.pool.specific += extra
            if ctx_hash:
                st["ctx_hash"] = ctx_hash
            log(f"  refilled {len(extra)} ideas from Gemini" + (" (your context changed)" if changed else ""))
    n = count or weighted([1, 2, 3], [80, 17, 3] if st.get("weekend") else cfg["drop_count_weights"])
    ratio = cfg["weekend_everyday_ratio"] if st.get("weekend") else cfg["everyday_ratio"]
    picks = ctx.pool.pick(n, used, ratio, cfg["quick_ratio"])
    keys = []
    for n_done, t in enumerate(picks):
        if n_done:
            pause(ctx, *(pace or (8, 40)))
        nodue = rng.random() < cfg["no_due_ratio"]
        due = None if nodue else ctx.day(rng.randint(0, 2)) if t["s"] == "q" else ctx.day(rng.randint(2, 9))
        desc = t["d"] or rng.choice(DESC[t["s"]])
        if due and t["s"] == "b" and rng.random() < 0.4:
            d = parse_day(due)
            desc += f"\nDeadline: {d.strftime('%A')} {d.day} {d.strftime('%B')}"
        slip = typo(t["t"]) if rng.random() < cfg["typo_chance"] else None
        key = create_task(ctx, t, due, desc, summary=slip)
        if key:
            if nodue:
                ctx.meta(key)["nd"] = 1
            keys.append(key)
    return keys


def create_task(ctx, t, due, desc, summary=None):
    fields = {"project": {"key": ctx.key}, "issuetype": {"name": ctx.issue_type},
              "summary": summary or t["t"], "description": adf(desc)}
    if ctx.cfg["label"]:
        fields["labels"] = [ctx.cfg["label"]]
    r = ctx.j.post("/rest/api/3/issue", {"fields": fields})
    key = r.get("key")
    log(f"  + {key or '(new)'} {summary or t['t']}")
    if not key:
        return None
    ctx.remember(key, {"a": t["a"], "s": t["s"], "d": due})
    if summary:
        ctx.meta(key)["ty"] = t["t"]  # fixed later, like a person spotting the typo
    ctx.touch(key)
    inbox = ctx.status.get("inbox")
    if inbox:
        cur = ctx.j.get(f"/rest/api/3/issue/{key}", fields="status")["fields"]["status"]["name"]
        if norm(cur) != norm(inbox):
            ctx.move(key, "inbox")
    return key


def phase_morning(ctx):
    log("Morning: deferred review and starting work")
    S = ctx.status
    if S.get("deferred"):
        extra = f"status = {jq(S['deferred'])}"
        if ctx.f.get("defer_until"):
            cf = ctx.f["defer_until"].split("_")[-1]
            extra += f" AND (cf[{cf}] <= startOfDay() OR cf[{cf}] is EMPTY)"
        else:
            extra += " AND updated <= -2d"
        for i in ctx.find(extra):
            breathe(ctx)
            if rng.random() < 0.7:
                ctx.move(i["key"], "process_today") or ctx.move(i["key"], "backlog")
            elif ctx.f.get("defer_until"):
                ctx.edit(i["key"], {ctx.f["defer_until"]: ctx.day(rng.randint(2, 6))})
                log(f"  {i['key']}: deferred again")
    if S.get("in_progress") and S.get("backlog"):  # too much on the go: put the stalest back for now
        meta = ctx.st.get("meta", {})
        wip = [i for i in ctx.find(f"status = {jq(S['in_progress'])}", order="ORDER BY updated ASC")
               if not ctx.is_real(i["key"]) and not meta.get(i["key"], {}).get("urg")]
        for i in wip[:max(0, len(wip) - ctx.cfg["wip_limit"] - 1)][:2]:
            breathe(ctx)
            if ctx.move(i["key"], "backlog"):
                if rng.random() < 0.5:
                    ctx.comment(i["key"], "push")
    start_next(ctx, rng.randint(1, 2))


def start_next(ctx, want):
    """Start up to `want` tasks without going over the work-in-progress limit:
    sprint tasks first, then anything due soon, then now and then something from the backlog.
    Returns the tasks it started."""
    S = ctx.status
    pool = ctx.sjql("backlog", "plan")
    if not pool or not S.get("in_progress"):
        return []
    wip = [i for i in ctx.find(f"status = {jq(S['in_progress'])}") if not ctx.is_real(i["key"])]
    room = min(want, ctx.cfg["wip_limit"] - len(wip))
    if room <= 0:
        return []
    cands = [i for i in ctx.find(pool) if not ctx.is_real(i["key"])]
    try:
        in_sprint = {i["key"] for i in ctx.find(f"{pool} AND sprint in openSprints()")}
    except JiraError:  # project without sprints
        in_sprint = set()
    soon = ctx.day(1)

    def rank(i):
        due = i["fields"].get("duedate") or "9999"
        return (i["key"] not in in_sprint, not (ctx.is_urgent(i) or due <= soon), due)
    cands.sort(key=rank)
    picks = [i for i in cands if i["key"] in in_sprint or (i["fields"].get("duedate") or "9999") <= soon]
    if not picks and cands and rng.random() < 0.5:
        picks = [rng.choice(cands)]
    started = []
    for i in picks[:room]:
        breathe(ctx)
        if start_work(ctx, i):
            started.append(i)
    return started


def phase_night_sort(ctx, lazy=False, limit=None, older=None):
    """Night, first run: triage the inbox and fill in the fields. Lazy nights only get through part of it."""
    log("Night: sorting the inbox")
    S, f = ctx.status, ctx.f
    process_keys = []
    if S.get("inbox"):
        inbox = ctx.find(f"status = {jq(S['inbox'])}" + (f" AND created <= -{older}d" if older else ""),
                         order="ORDER BY created ASC")[:limit or ctx.cfg["sort_limit"]]
        if lazy and len(inbox) > 1:
            inbox = inbox[:max(1, int(len(inbox) * rng.uniform(0.3, 0.6)))]
            log(f"  lazy night, sorting {len(inbox)} for now")
        for i in inbox:
            breathe(ctx)
            k, inf = i["key"], ctx.info(i)
            if not i["fields"].get("parent"):
                epic = ctx.epic_for(inf["a"])
                if epic:
                    ctx.edit(k, {"parent": {"key": epic}})
                    pause(ctx, 1.5, 5)
            real = ctx.is_real(k)
            if not real and inf["s"] == "q" and rng.random() < 0.3:
                if ctx.finish(k):  # quick enough to just do instead of filing it
                    ctx.log_work(k, rng.uniform(0.1, 0.4))
            elif not real and S.get("deferred") and rng.random() < 0.2:
                if f.get("defer_until"):
                    ctx.edit(k, {f["defer_until"]: ctx.day(rng.randint(1, 5))})
                ctx.move(k, "deferred")
            elif ctx.move(k, "process_today"):
                process_keys.append(k)
            elif ctx.move(k, "backlog"):
                process_keys.append(k)

    todo = []
    if S.get("process_today"):
        todo += ctx.find(f"status = {jq(S['process_today'])}", order="ORDER BY created ASC")
    if not S.get("inbox") and S.get("backlog"):  # no inbox status: sort anything new that is not filled in yet
        meta = ctx.st.get("meta", {})
        for i in ctx.find(f"status = {jq(S['backlog'])} AND created >= -2d")[:limit or ctx.cfg["sort_limit"]]:
            if meta.get(i["key"], {}).get("f"):
                continue
            if not ctx.is_real(i["key"]) and ctx.info(i)["s"] == "q" and rng.random() < 0.3:
                breathe(ctx)
                if ctx.finish(i["key"]):  # quick enough to just do instead of filing it
                    ctx.log_work(i["key"], rng.uniform(0.1, 0.4))
                    continue
            todo.append(i)
    seen = {i["key"] for i in todo}
    for k in process_keys:
        if k not in seen:
            issue = ctx.j.get(f"/rest/api/3/issue/{k}", fields=",".join(ctx.search_fields()))
            todo.append(issue if issue.get("key") else {"key": k, "fields": {}})
    process(ctx, todo, gap=(3, 10))  # filling in fields right after triage goes quicker


def process(ctx, todo, allow_later=True, gap=None):
    """Fill due date, assignee, epic, component, points and sizes, then file each task in the backlog."""
    S, f = ctx.status, ctx.f
    for n_done, i in enumerate(todo):
        if gap and n_done:
            pause(ctx, *gap)
        k, fl, inf = i["key"], i.get("fields") or {}, ctx.info(i)
        due = fl.get("duedate") or inf.get("d")
        if not due and not inf.get("nd"):
            due = ctx.day(rng.randint(0, 2)) if inf["s"] == "q" else ctx.day(rng.randint(2, 9))
        urgent = bool(due) and due <= ctx.day(2)
        quick = inf["s"] == "q"
        study = ctx.pool.kind(inf["a"]) in ("course", "project")
        pts = (fl.get(f["story_points"]) if f.get("story_points") else None) or inf.get("p") or \
            (1 if quick else rng.choice([2, 3, 3, 5, 5, 8] if study else [1, 2, 2, 3]))
        if k in ctx.st.get("meta", {}):
            ctx.meta(k)["p"] = pts
        fields = {
            "duedate": due,
            "assignee": {"accountId": ctx.me},
            "components": None if fl.get("components") else ctx.component_for(inf["a"]),
            "priority": ctx.priority(k, urgent),
        }
        if not fl.get("parent"):
            e = ctx.epic_for(inf["a"])
            fields["parent"] = {"key": e} if e else None
        if f.get("story_points") and not fl.get(f["story_points"]):
            fields[f["story_points"]] = pts
        if f.get("deep_shallow"):
            deep = (inf["m"] >= 45) if inf.get("m") else (not quick) and rng.random() < 0.75
            fields[f["deep_shallow"]] = ctx.option(k, "deep_shallow", ["deep"] if deep else ["shallow"])
        if f.get("task_size"):
            fields[f["task_size"]] = ctx.option(k, "task_size",
                                                ["small", "quick", "<"] if quick else ["big", "large", ">"])
        ctx.edit(k, fields)
        if k in ctx.st.get("meta", {}):
            ctx.meta(k)["f"] = 1
        log(f"  {k}: fields filled (due {due or 'none'})")
        pause(ctx, 1.5, 5)
        title = norm(fl.get("summary"))
        if any(w in title for w in PLAN_WORDS) and S.get("plan"):
            ctx.move(k, "plan")
        elif allow_later and not ctx.is_real(k) and (not quick) and S.get("later") and rng.random() < 0.06:
            ctx.move(k, "later")
        elif S.get("process_today"):
            ctx.move(k, "backlog")


def phase_night_work(ctx, capacity, active=False):
    """Night, second run: spend today's hours. Tasks in progress move on (bigger ones take days and
    log time), finished work goes to review first, the next task starts when one finishes, small
    chores get ticked off, past events close, and missed deadlines get pushed."""
    log(f"Night: working through tasks ({capacity:.1f}h today)")
    S = ctx.status
    today = ctx.day()
    meta = ctx.st.setdefault("meta", {})
    mine = lambda issues: [i for i in issues if not ctx.is_real(i["key"]) and not meta.get(i["key"], {}).get("urg")]
    study = lambda i: ctx.pool.kind(ctx.info(i).get("a", "")) in ("course", "project")

    if S.get("review"):  # yesterday's reviews get closed, now and then something needs another pass
        for i in mine(ctx.find(f"status = {jq(S['review'])}")):
            m = ctx.meta(i["key"])
            if m.get("rv") == today or (active and rng.random() < 0.5):
                continue
            breathe(ctx)
            r = rng.random()
            if r < 0.85:
                if ctx.finish(i["key"]) and rng.random() < 0.25:
                    ctx.comment(i["key"], "done")
            elif r < 0.93 and len(mine(ctx.find(f"status = {jq(S['in_progress'])}"))) < ctx.cfg["wip_limit"] \
                    and ctx.move(i["key"], "in_progress"):
                m["h"] = round(rng.uniform(0.5, 1.5), 2)
                ctx.comment(i["key"], "reopen")

    # Search once, then keep a local list: Jira's search can lag a few seconds behind a change,
    # and re-searching after a start could start task after task.
    queue = mine(ctx.find(f"status = {jq(S['in_progress'])}", order="ORDER BY duedate ASC, updated ASC")) \
        if S.get("in_progress") else []  # whatever is due soonest gets the evening first
    starts = 0
    while capacity >= 0.25:  # keep going until the evening's hours run out
        if not queue:
            if capacity < 0.75 or active or starts >= 2:
                break
            new = start_next(ctx, 1)
            if not new:
                break
            starts += 1
            queue += new
            continue
        i = queue.pop(0)
        k, m = i["key"], ctx.meta(i["key"])
        breathe(ctx)
        left = m.get("h") or effort_hours(ctx, i)
        share = capacity if not queue else capacity * rng.uniform(0.5, 0.8)
        share = min(share, rng.uniform(1.5, 2.75))  # nobody does five straight hours on one task
        spend = max(0.25, round(min(left, share) * 4) / 4)
        capacity -= spend
        left = round(left - spend, 2)
        ctx.log_work(k, spend)
        if left <= 0.1:
            complete(ctx, i)
        else:
            m["h"] = left
            if spend >= 1 and rng.random() < 0.25:
                ctx.comment(k, "progress")

    pool = ctx.sjql("backlog", "plan", "process_today")
    if not pool or active:
        return
    old = ctx.day(-3)
    small = [i for i in mine(ctx.find(pool)) if not study(i) and
             (ctx.info(i).get("s") == "q" or (ctx.info(i).get("p") or 2) <= 2) and
             ((i["fields"].get("duedate") or "9999") <= today or
              (not i["fields"].get("duedate") and str(i["fields"].get("created", ""))[:10] <= old))]
    for i in small:  # chores and small errands just get done, no In Progress needed
        if capacity < 0.25:
            break
        if rng.random() > 0.8:
            continue
        info = ctx.info(i)
        spend = round(rng.uniform(0.15, 0.5) if info.get("s") == "q" else
                      HOURS.get(info.get("p") or 2, 1.5) * rng.uniform(0.6, 1.1), 2)
        if spend > capacity + 0.25:
            continue
        breathe(ctx)
        if ctx.finish(i["key"]):
            capacity -= spend
            ctx.log_work(i["key"], spend)
            if rng.random() < 0.1:
                ctx.comment(i["key"], "done")
    for i in mine(ctx.find(f"{ctx.sjql('backlog', 'plan')} AND duedate < startOfDay()")):
        if any(w in norm(i["fields"].get("summary")) for w in PLAN_WORDS) and rng.random() < 0.9:
            breathe(ctx)  # the meeting or match has happened
            ctx.finish(i["key"])
    overdue = [i for i in mine(ctx.find(f"{ctx.sjql('backlog', 'plan')} AND duedate < startOfDay()"))
               if ctx.info(i).get("s") != "q" and not any(w in norm(i["fields"].get("summary")) for w in PLAN_WORDS)]
    for i in overdue[:2]:  # missed a deadline: push it a few days, like anyone would
        breathe(ctx)
        new = ctx.day(rng.randint(1, 4))
        ctx.edit(i["key"], {"duedate": new})
        ctx.meta(i["key"])["d"] = new
        if rng.random() < 0.5:
            ctx.comment(i["key"], "push")
    stale_cut = (ctx.now() - timedelta(days=14)).isoformat()
    stale = [i for i in mine(ctx.find(ctx.sjql("backlog"), order="ORDER BY updated ASC"))
             if (i["fields"].get("updated") or "9999") < stale_cut]
    for i in stale[:rng.randint(0, 2)]:  # a bit of spring cleaning: old life tasks were done long ago
        breathe(ctx)
        if not study(i) and rng.random() < 0.6:
            ctx.finish(i["key"])
        elif S.get("later"):
            ctx.move(i["key"], "later")
        elif rng.random() < 0.4:
            ctx.finish(i["key"])


def complete(ctx, issue):
    k = issue["key"]
    m = ctx.meta(k)
    m.pop("h", None)
    if ctx.status.get("review") and m.get("s") != "q" and rng.random() < 0.55 and ctx.move(k, "review"):
        m["rv"] = ctx.day()
        if rng.random() < 0.4:
            ctx.comment(k, "review")
        return
    if ctx.finish(k) and rng.random() < 0.35:
        ctx.comment(k, "done")


def phase_night_wrap(ctx, plan, active=False):
    """Night, third run: sprint upkeep, typo fixes, and a weekly refresh of comment phrases."""
    log("Night: wrapping up")
    phase_sprint(ctx, plan=plan)
    for k, m in ([] if active else list(ctx.st.get("meta", {}).items())):
        if m.get("ty") and not m.get("x"):
            breathe(ctx)
            ctx.edit(k, {"summary": m.pop("ty")})
            log(f"  {k}: fixed a typo in the title")
    ts = utcnow().timestamp()
    if gemini_key() and ts >= ctx.st.get("bank_after", 0):
        ctx.st["bank_after"] = ts + 7 * 86400
        bank = llm_comments(ctx.personal, ctx.cfg["gemini_model"])
        if bank:
            ctx.st["bank"] = bank[:60]
            log(f"  refreshed {len(bank)} comment phrases from Gemini")


def phase_edits(ctx):
    """One small tidy-up a person makes during the day: re-rank, nudge a due date or bump a priority."""
    log("Edit: small tidy-up")
    pool = ctx.sjql("backlog", "plan")
    cands = [i for i in ctx.find(pool) if not ctx.is_real(i["key"])] if pool else []
    if len(cands) < 2:
        return
    choice = weighted(["rank", "due", "priority"], [4, 4, 2])
    if choice == "rank":
        a, b = rng.sample(cands, 2)
        try:
            ctx.j.put("/rest/agile/1.0/issue/rank", {"issues": [a["key"]], "rankBeforeIssue": b["key"]})
            ctx.touch(a["key"])
            log(f"  moved {a['key']} above {b['key']}")
        except JiraError:
            pass
    elif choice == "due":
        dated = [i for i in cands if (i["fields"].get("duedate") or "") > ctx.day(1)]
        if dated:
            i = rng.choice(dated)
            new = max(ctx.day(1), (parse_day(i["fields"]["duedate"]) +
                                   timedelta(days=rng.choice([-2, -1, 1, 1, 2, 3]))).isoformat())
            ctx.edit(i["key"], {"duedate": new})
            ctx.meta(i["key"])["d"] = new
            log(f"  {i['key']}: due date moved to {new}")
    else:
        soon = [i for i in cands if (i["fields"].get("duedate") or "9999") <= ctx.day(2) and not ctx.is_urgent(i)]
        if soon:
            i = rng.choice(soon)
            ctx.edit(i["key"], {"priority": ctx.priority(i["key"], True)})
            log(f"  {i['key']}: bumped the priority")


def find_board(ctx):
    """The scrum board to use: board_id from config, else the first one that already has sprints."""
    if ctx.cfg.get("board_id"):
        return ctx.cfg["board_id"]
    boards = ctx.j.get("/rest/agile/1.0/board", projectKeyOrId=ctx.key, type="scrum").get("values", [])
    for bd in boards:
        if ctx.j.get(f"/rest/agile/1.0/board/{bd['id']}/sprint", maxResults=1).get("values"):
            return bd["id"]
    return boards[0]["id"] if boards else None


def phase_sprint(ctx, plan=True):
    """Close the sprint once its end date has passed and start the next one. With plan, also
    pull urgent work into the running sprint or start a sprint when none is running."""
    b = find_board(ctx)
    if not b:
        log("  no scrum board in this project, skipping sprints")
        return
    active = ctx.j.get(f"/rest/agile/1.0/board/{b}/sprint", state="active").get("values", [])
    if active:
        s = active[0]
        end = parse_dt(s.get("endDate"))
        if end and end <= ctx.now() + timedelta(hours=2):
            log("Sprint: closing and planning the next one")
            meta = ctx.st.get("meta", {})
            done = sum(meta.get(i["key"], {}).get("p") or 2
                       for i in ctx.find(f"sprint = {s['id']} AND statusCategory = Done"))
            full = not ctx.find(f"sprint = {s['id']} AND statusCategory != Done")
            if full:  # finished everything, so there was room for more than was planned
                done = max(done * 1.4, ctx.st.get("sprint_budget", 0))
            old = ctx.st.get("velocity")
            ctx.st["velocity"] = round(done if old is None or full and done > old else 0.5 * old + 0.5 * done, 1)
            log(f"  finished {done} points, planning about {ctx.st['velocity']} next")
            ctx.j.post(f"/rest/agile/1.0/sprint/{s['id']}", {"state": "closed"})
            log(f"  closed sprint '{s['name']}'")
            start_sprint(ctx, b, carry=s["id"])
            ctx.st["last_sprint_day"] = ctx.day()
        elif plan:
            log("Sprint: upkeep")
            keep_sprint(ctx, s)
            ctx.st["last_sprint_day"] = ctx.day()
    elif plan or not ctx.st.get("last_sprint_day"):
        log("Sprint: planning")
        if start_sprint(ctx, b):
            ctx.st["last_sprint_day"] = ctx.day()


def start_sprint(ctx, board, carry=None):
    scope = ctx.sjql("backlog", "plan", "in_progress", "review")
    started = ctx.sjql("in_progress", "review")  # only work already under way carries over automatically
    carried = ctx.find(f"sprint = {carry} AND {started}") if carry and started else []
    others = []
    if scope:
        taken = {c["key"] for c in carried}
        stale = ctx.day(-1)  # your own tasks long past their date are yours to sort out, not to plan
        others = [i for i in ctx.find(f"{scope} AND statusCategory != Done") if i["key"] not in taken
                  and not (ctx.is_real(i["key"]) and (i["fields"].get("duedate") or "9999") < stale)]
    others.sort(key=lambda i: (not ctx.is_urgent(i), i["fields"].get("duedate") or "9999"))
    ordered = carried + others
    if len(ordered) < 3:
        log("  not enough processed work for a sprint yet")
        return False
    future = sorted(ctx.j.get(f"/rest/agile/1.0/board/{board}/sprint", state="future").get("values", []),
                    key=lambda s: s["id"])
    if future:
        sp = future[0]
    else:
        total = ctx.j.get(f"/rest/agile/1.0/board/{board}/sprint", maxResults=50).get("total", 0)
        name = f"{ctx.cfg['sprint_name_prefix'] or ctx.key} Sprint {total + 1}"
        sp = ctx.j.post("/rest/agile/1.0/sprint", {"name": name, "originBoardId": board}) or {"id": 0, "name": name}
        log(f"  created sprint '{sp['name']}'")
    lo, hi = ctx.cfg["sprint_size"]
    target, chosen, points, sp_field = rng.randint(lo, hi), [], 0, ctx.f.get("story_points")
    budget = max(8, min(60, (ctx.st.get("velocity") or rng.uniform(18, 26)) * rng.uniform(1.1, 1.4)))  # a little optimistic, like everyone
    ctx.st["sprint_budget"] = round(budget, 1)
    estimates = {}
    for i in ordered:
        if points >= budget or len(chosen) >= target + 6:  # the point budget sets the size, the count is a ceiling
            break
        est = (i["fields"].get(sp_field) if sp_field else None) or ctx.st.get("meta", {}).get(i["key"], {}).get("p")
        if not est:
            est = rng.choice([1, 2, 3, 5])
            estimates[i["key"]] = est
        points += est
        chosen.append(i["key"])
    start = ctx.now()
    end = (start + timedelta(days=ctx.cfg["sprint_length_days"])).replace(hour=18, minute=0, second=0, microsecond=0)
    for k, v in estimates.items():
        ctx.meta(k)["p"] = v
        if sp_field:
            ctx.edit(k, {sp_field: v})
    for i in ordered:  # planning is when undated work gets a date, about half the time
        if i["key"] in chosen and not i["fields"].get("duedate") and not ctx.is_real(i["key"]) and rng.random() < 0.5:
            due = (start + timedelta(days=rng.randint(2, ctx.cfg["sprint_length_days"]))).date().isoformat()
            ctx.edit(i["key"], {"duedate": due})
            ctx.meta(i["key"])["d"] = due
    ctx.add_to_sprint(sp["id"], chosen)
    themes = []
    for i in ordered:
        if i["key"] in chosen and i["fields"].get("parent"):
            name = i["fields"]["parent"].get("fields", {}).get("summary")
            if name and name not in themes:
                themes.append(name)
    goal = sp.get("goal") or (  # a sprint you planned yourself keeps your goal
        rng.choice(["Get through {0} and keep {1} moving", "Focus on {0}, then {1}", "Clear {0} and push {1}"])
        .format(themes[0], themes[1]) if len(themes) >= 2 else
        f"Make solid progress on {themes[0]}" if themes else "Clear the most urgent work this week")
    ctx.j.post(f"/rest/agile/1.0/sprint/{sp['id']}", {
        "name": sp["name"], "state": "active", "goal": goal,
        "startDate": start.isoformat(timespec="seconds"), "endDate": end.isoformat(timespec="seconds")})
    log(f"  started '{sp['name']}' with {len(chosen)} issues, {points} points. Goal: {goal}")
    return True


def keep_sprint(ctx, s):
    """Mid-sprint: pull in one genuinely urgent task, but only while the sprint is not already full."""
    inside = ctx.find(f"sprint = {s['id']}")
    meta = ctx.st.get("meta", {})
    left = sum(meta.get(i["key"], {}).get("p") or 2 for i in inside if i["fields"]["status"]["name"] != ctx.status.get("done"))
    room = (ctx.st.get("velocity") or 15) * 0.6 - left
    scope = ctx.sjql("backlog", "plan")
    if scope and room > 0:
        outside = ctx.find(f"{scope} AND (sprint is EMPTY OR sprint not in openSprints())")
        hot = [i for i in outside if ctx.is_urgent(i) and (i["fields"].get("duedate") or "9999") >= ctx.day()]
        if hot:
            ctx.add_to_sprint(s["id"], [hot[0]["key"]])
            log(f"  added {hot[0]['key']} to '{s['name']}'")
    log(f"  sprint '{s['name']}' mid-way, {len(inside)} issues, {left} points left")


def phase_kickoff(ctx):
    """First run only: jot down a few tasks, sort them, plan a sprint and start on one,
    the way someone sets up a fresh board, instead of waiting two nights for a sprint."""
    log("Kickoff: first run, setting up the board")
    ctx.st["kickoff_done"] = True  # set first so a failure halfway never repeats the drop
    if ctx.cfg["allow_create_epics"]:
        ctx.epic_budget = 3
    keys = phase_drop(ctx, rng.randint(*ctx.cfg["kickoff_tasks"]), pace=(10, 40))
    if keys:
        pause(ctx, 30, 60)
        settle(ctx, keys)


def settle(ctx, keys):
    """Second half of the kickoff: sort the new tasks, start a sprint, start one task."""
    log("  sorting the new tasks")
    todo = []
    for n, k in enumerate(keys):
        if n:
            breathe(ctx)
        if not leave_inbox(ctx, k):
            continue
        todo.append(ctx.j.get(f"/rest/api/3/issue/{k}", fields=",".join(ctx.search_fields())))
    pause(ctx, 15, 40)
    process(ctx, todo, allow_later=False, gap=(3, 10))
    quick = [i["key"] for i in todo if ctx.info(i)["s"] == "q"]
    if len(todo) >= 4 and quick and rng.random() < 0.6:
        pause(ctx, 15, 40)
        k = rng.choice(quick)
        if ctx.finish(k):  # a quick one gets ticked off straight away
            ctx.comment(k, "done")
    pause(ctx, 20, 45)
    phase_sprint(ctx)
    pause(ctx, 15, 40)
    start_next(ctx, 1)


def leave_inbox(ctx, k):
    """Give a new task its epic and move it out of the inbox. False if Jira refuses."""
    inbox = ctx.status.get("inbox")
    if not inbox:
        return True
    cur = ctx.j.get(f"/rest/api/3/issue/{k}", fields="status")["fields"]["status"]["name"]
    if norm(cur) != norm(inbox):
        return True  # already sorted
    epic = ctx.epic_for(ctx.st.get("meta", {}).get(k, {}).get("a", "self"))
    if epic:  # some workflows refuse to leave the inbox without an epic
        ctx.edit(k, {"parent": {"key": epic}})
    return ctx.move(k, "process_today") or ctx.move(k, "backlog")


def plan_urgent(ctx):
    """Rolled once a day: some days get one task that suddenly has to be done today."""
    if (ctx.st.get("urgent") or {}).get("stage") in ("working", "review"):
        return  # yesterday's is still in flight
    lo, hi = ctx.cfg["active_hours"]
    at = None
    if not ctx.st.get("quiet") and rng.random() < ctx.cfg["urgent_day_chance"]:
        at = rand_time([f"{lo:02d}:30", f"{max(lo, hi - 3):02d}:00"])
    ctx.st["urgent"] = {"at": at, "stage": "planned" if at else None}


def phase_urgent(ctx):
    """Something comes up: add it, pull it into the running sprint and start on it straight away."""
    log("Urgent: something came up")
    u = ctx.st.setdefault("urgent", {})
    u["stage"] = None  # one attempt per day, even if Jira refuses part of it
    recent = ctx.j.search(f"project = {jq(ctx.key)} AND created >= -21d", ["summary"], 300)
    t = ctx.pool.pick_urgent({norm(i["fields"]["summary"]) for i in recent})
    if not t:
        log("  no urgent ideas left")
        return
    key = create_task(ctx, t, ctx.day(), rng.choice(DESC["u"]))
    if not key:
        return
    ctx.meta(key)["urg"] = 1
    if not leave_inbox(ctx, key):
        return
    pause(ctx, 20, 60)
    process(ctx, [ctx.j.get(f"/rest/api/3/issue/{key}", fields=",".join(ctx.search_fields()))], allow_later=False)
    b = find_board(ctx)
    active = ctx.j.get(f"/rest/agile/1.0/board/{b}/sprint", state="active").get("values", []) if b else []
    if active:
        ctx.add_to_sprint(active[0]["id"], [key])
        log(f"  added {key} to '{active[0]['name']}'")
    pause(ctx, 20, 60)
    if ctx.move(key, "in_progress"):
        ctx.comment(key, "urgent")
        u.update(stage="working", key=key, t0=utcnow().timestamp(),
                 next=utcnow().timestamp() + rng.uniform(*ctx.cfg["urgent_work_hours"]) * 3600)


def phase_urgent_followup(ctx):
    """A few hours later: send the urgent task for review and close it, or now and then let it slip a day."""
    u, S = ctx.st["urgent"], ctx.status
    k = u["key"]
    log(f"Urgent: following up on {k}")
    cur = norm(ctx.j.get(f"/rest/api/3/issue/{k}", fields="status")["fields"]["status"]["name"])
    if S.get("done") and cur == norm(S["done"]):
        u["stage"] = "closed"
        return
    if u["stage"] == "working":
        worked = (utcnow().timestamp() - u.get("t0", utcnow().timestamp())) / 3600
        spent = max(0.5, min(3.0, worked * rng.uniform(0.5, 0.8)))
        ctx.log_work(k, spent)
        ctx.st["hours"] = round(max(0.0, ctx.st.get("hours", 0) - spent), 2)  # less left for the evening
        if not u.get("slipped") and rng.random() < ctx.cfg["urgent_slip_chance"]:
            ctx.edit(k, {"duedate": ctx.day(1)})  # did not make it today, finish it tomorrow
            ctx.comment(k, "push")
            u.update(slipped=1, next=utcnow().timestamp() + rng.uniform(16, 22) * 3600)
            return
        if S.get("review") and ctx.move(k, "review"):
            ctx.comment(k, "review")
            u.update(stage="review", next=utcnow().timestamp() + rng.uniform(0.3, 1.5) * 3600)
            return
    if ctx.finish(k):
        ctx.comment(k, "done")
    u["stage"] = "closed"


# ---------------------------------------------------------------- your own tasks (GitHub issue inbox)

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DUR_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:hours|hour|hrs|hr|h)(?:\b|(?=\d))(?:\s*(\d+)\s*(?:m|min|mins|minutes)\b)?"
                    r"|\b(\d+)\s*(?:m|min|mins|minute|minutes)\b", re.I)
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DUE_RE = re.compile(r"\b(?:(?:by|on|due|before)\s+)?(today|tonight|tomorrow|tmrw|tmr|next week|in \d+ days?|"
                    + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) +
                    rf"|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\s+{_MON}|{_MON}\s+\d{{1,2}}|\d{{1,2}}/\d{{1,2}})\b", re.I)


def parse_minutes(text):
    m = DUR_RE.search(text or "")
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    return round(float(m.group(1)) * 60 + int(m.group(2) or 0))


def parse_when(ctx, text):
    """'today', 'tmrw', 'fri', 'in 3 days', '24 oct', 'oct 24', '24/10', '2026-10-24' -> ISO date."""
    t = " ".join((text or "").lower().replace(",", " ").split()).removeprefix("by ").removeprefix("on ").removeprefix("due ").removeprefix("before ").strip()
    today = ctx.today()
    if t in ("today", "tonight", "now", "asap"):
        return ctx.day()
    if t in ("tomorrow", "tmrw", "tmr"):
        return ctx.day(1)
    if t == "next week":
        return ctx.day(7)
    m = re.fullmatch(r"(?:in )?(\d+) days?", t)
    if m:
        return ctx.day(int(m.group(1)))
    if t in WEEKDAYS:
        return ctx.day((WEEKDAYS[t] - today.weekday()) % 7)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t
    m = re.fullmatch(r"(\d{1,2}) ([a-z]+)|([a-z]+) (\d{1,2})|(\d{1,2})/(\d{1,2})", t)
    if not m:
        return None
    if m.group(5):
        d, mon = int(m.group(5)), int(m.group(6))
    else:
        d, name = (int(m.group(1)), m.group(2)) if m.group(1) else (int(m.group(4)), m.group(3))
        if name[:3] not in MONTHS:
            return None
        mon = MONTHS.index(name[:3]) + 1
    try:
        when = today.replace(month=mon, day=d)
    except ValueError:
        return None
    if when < today:
        when = when.replace(year=when.year + 1)
    return when.isoformat()


def points_for(minutes):
    return 1 if minutes <= 15 else 2 if minutes <= 60 else 3 if minutes <= 150 else 5 if minutes <= 300 else 8


def split_line(ctx, line):
    """'Finish DB lab | 2h | fri | DBMS' or free text like 'Finish DB lab by fri, 2h' -> (title, minutes, due, area)."""
    if "|" in line:
        parts = [x.strip() for x in line.split("|")]
        title, minutes, due, area = parts[0], None, None, None
        for x in parts[1:]:
            if minutes is None and parse_minutes(x) is not None:
                minutes = parse_minutes(x)
            elif due is None and parse_when(ctx, x):
                due = parse_when(ctx, x)
            elif x:
                area = x
        return title, minutes, due, area
    title, minutes, due = line, None, None
    m = DUR_RE.search(title)
    if m:
        minutes, title = parse_minutes(m.group(0)), title[:m.start()] + title[m.end():]
    m = DUE_RE.search(title)
    at_end = m and not title[m.end():].strip(" ,;.)")
    prefixed = m and re.match(r"(?:by|on|due|before)\s", m.group(0), re.I)
    if m and (at_end or prefixed) and parse_when(ctx, m.group(0)):
        due, title = parse_when(ctx, m.group(0)), title[:m.start()] + title[m.end():]
    title = re.sub(r"\s{2,}", " ", re.sub(r"[\s,;:(\-]+$|\(\s*\)", "", title)).strip(" ,;-")
    return title, minutes, due, None


def llm_inbox(ctx, text):
    prompt = (f"Today is {ctx.today().strftime('%A %d %B %Y')}. Turn this to-do text into tasks.\n"
              "Return ONLY a JSON array of [title, minutes or null, due date YYYY-MM-DD or null]. "
              "Titles 3 to 9 words, the way a student types a to-do. Keep every task, add none.\n\n" + text)
    items = llm_array(prompt, ctx.cfg["gemini_model"]) or []
    out = []
    for it in items:
        if isinstance(it, list) and it and isinstance(it[0], str) and it[0].strip():
            minutes = it[1] if len(it) > 1 and isinstance(it[1], (int, float)) else None
            due = it[2] if len(it) > 2 and isinstance(it[2], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", it[2]) else None
            out.append((it[0].strip(), int(minutes) if minutes else None, due, None))
    return out


def write_result(report):
    out = os.environ.get("INBOX_RESULT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"- {r}" for r in report) + "\n")
    for r in report:
        log(f"  {r}")


def phase_inbox(ctx, title, body):
    """Your own tasks, written as a GitHub issue. One task per line. Lines starting with
    'done' close a task you added earlier, by key or by title."""
    log("Inbox: adding your own tasks")
    text = body if (body or "").strip() else title
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])?\s*(?:\[[ xX]?\])?\s*", "", raw).strip()
        if line and not line.endswith(":") and not line.startswith("#"):
            lines.append(line)
    done = [re.sub(r"^(?:done|finished|completed?)\b\s*[:\-]?\s*", "", l, flags=re.I) for l in lines
            if re.match(r"(?:done|finished|completed?)\b", l, re.I)]
    todo = [l for l in lines if not re.match(r"(?:done|finished|completed?)\b", l, re.I)][:30]
    report = []

    if done:
        mine = [i for i in ctx.find("statusCategory != Done") if ctx.is_real(i["key"])]
        for want in done:
            key = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", want)
            hit = next((i for i in mine if key and i["key"] == key.group(0)), None)
            if not hit:
                w = tokens(want)
                scored = [(len(w & tokens(i["fields"]["summary"])) / max(1, len(w)), i) for i in mine]
                best = max(scored, key=lambda x: x[0], default=(0, None))
                hit = best[1] if best[0] >= 0.6 else None
            if hit and ctx.finish(hit["key"]):
                ctx.comment(hit["key"], "done")
                report.append(f"Closed {hit['key']} {hit['fields']['summary']}")
            else:
                report.append(f"Could not find an open task of yours matching \"{want}\"")

    items = []
    if todo:
        if not any("|" in l for l in todo):
            items = llm_inbox(ctx, "\n".join(todo))
        items = items or [split_line(ctx, l) for l in todo]
    for title_, minutes, due, area in items:
        if not title_:
            continue
        title_ = title_[0].upper() + title_[1:]
        size = "q" if minutes is not None and minutes <= 15 else "b"
        due = due or (ctx.day(rng.randint(0, 2)) if size == "q" else ctx.day(rng.randint(2, 7)))
        if minutes:
            hours = minutes / 60
            desc = f"About {minutes} minutes." if minutes < 60 else \
                f"About {hours:g} hour{'s' if hours != 1 else ''}." if hours == int(hours) or hours < 3 \
                else f"About {round(hours)} hours."
        else:
            desc = rng.choice(DESC[size])
        t = {"t": title_[:200], "s": size, "a": area or ctx.guess_area(title_), "d": ""}
        key = create_task(ctx, t, due, desc)
        if not key:
            if ctx.j.dry:
                report.append(f"Would add {title_}, due {due}" + (f", {desc[6:-1]}" if minutes else ""))
            continue
        ctx.meta(key).update(real=True, m=minutes, p=points_for(minutes) if minutes else None)
        when = parse_day(due)
        report.append(f"Added {key} {title_}, due {when.strftime('%a %d %b')}" +
                      (f", {desc[6:-1]}" if minutes else ""))
    if not report:
        report.append("Nothing to add. Write one task per line, like: Finish DB lab | 2h | fri")
    write_result(report)


SETUP_RULES = """Rules:
- 70 tasks. "q" = quick (under 10 minutes), "b" = bigger. About 40% quick.
- area = one of my course names, one of my project names, or one of:
  chores, errands, tech, social, family, health, self, admin, finance, career.
- About 60% about my courses and projects, 40% my hobbies and routine.
- Titles 3 to 9 words, the way a student would type a to-do. Specific and varied, no duplicates.
- Where my existing epics fit, use the same course or project names they use.
- 12 urgent tasks: things that suddenly have to be done TODAY. A deadline tonight, a quiz moved to tomorrow,
  a bug before a demo, a form closing at 5pm. Say why it is urgent in the title. Same areas as above."""


def cmd_setup(ctx, about):
    """Write tasks.json from an 'About me' issue, in one Gemini call."""
    log("Setup: writing your personal task list")
    if not gemini_key():
        return write_result(["Setup needs a GEMINI_API_KEY secret. Add one, then open this issue again. "
                             "Or follow SETUP_PROMPT.md by hand."])
    if len((about or "").strip()) < 30:
        return write_result(["Tell me a bit more: your uni and year, your courses (with short names), "
                             "your projects, and what a normal week looks like."])
    open_ = ctx.j.search(f"{ctx.base()} AND statusCategory != Done ORDER BY updated DESC", ["summary"], 25)
    prompt = (
        "Make a tasks.json file for my personal Jira board. Output ONLY valid JSON, no explanation.\n\n"
        f"About me:\n{about.strip()}\n\nMy current Jira:\n"
        f"EPICS: {'; '.join(e['summary'] for e in ctx.epics[:40])}\n"
        f"COMPONENTS: {'; '.join(ctx.components)}\n"
        f"RECENT OPEN TASKS: {'; '.join(i['fields']['summary'] for i in open_)}\n\n"
        'Schema:\n{"profile": "<one line about me>", "courses": [{"name": "<full name>", "short": "<short name>"}], '
        '"projects": [{"name": "<full name>", "short": "<short name>"}], '
        '"tasks": [["<title>", "q" or "b", "<area>"]], "urgent": [["<title>", "q" or "b", "<area>"]]}\n' + SETUP_RULES)
    data = llm_json(prompt, ctx.cfg["gemini_model"], "{")
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return write_result(["Gemini did not return a usable task list. Try again, or follow SETUP_PROMPT.md by hand."])

    def clean(rows):
        out, seen = [], set()
        for r in rows or []:
            t = mk_task(r)
            if t["t"] and norm(t["t"]) not in seen and t["s"] in ("q", "b"):
                seen.add(norm(t["t"]))
                out.append([t["t"][:120], t["s"], t["a"]])
        return out
    tasks = {"profile": str(data.get("profile", ""))[:200],
             "courses": [c for c in data.get("courses", []) if isinstance(c, dict) and c.get("name")],
             "projects": [p for p in data.get("projects", []) if isinstance(p, dict) and p.get("name")],
             "tasks": clean(data.get("tasks")), "urgent": clean(data.get("urgent"))}
    if len(tasks["tasks"]) < 20:
        return write_result([f"Only {len(tasks['tasks'])} usable tasks came back. Try again with more detail."])
    path = os.environ.get("TASKS_FILE") or os.path.join(HERE, "tasks.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tasks, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    names = ", ".join((c.get("short") or c["name"]) for c in tasks["courses"] + tasks["projects"])
    write_result([f"Saved tasks.json with {len(tasks['tasks'])} tasks and {len(tasks['urgent'])} urgent ones.",
                  f"Courses and projects: {names or 'none'}.",
                  "New drops use it from the next run. Open another setup issue any time to redo it."])


# ---------------------------------------------------------------- scheduler

def new_day(ctx):
    """Plan the day the way a person's day goes: weekends are lighter, some days nothing happens."""
    st, cfg = ctx.st, ctx.cfg
    today = ctx.today()
    weekend = today.weekday() in cfg["weekend_days"]
    quiet = day_off(cfg, today) or (not st.get("quiet") and rng.random() < cfg["quiet_day_chance"])  # never two in a row
    lo, hi = cfg["weekend_hours"] if weekend else cfg["daily_hours"]
    st.update(day=ctx.day(), weekend=weekend, quiet=quiet, lazy=rng.random() < cfg["lazy_night_chance"],
              hours=0 if quiet else round(rng.uniform(lo, hi), 2),
              morning_at=rand_time(cfg["weekend_morning_window" if weekend else "morning_window"]),
              night_at=rand_time(cfg["night_window"]), morning_done=False, night_stage=0, night_next=0,
              edit_at=rand_time(["11:00", "20:00"]) if rng.random() < cfg["edit_chance"] else None)
    first = hhmm(st["morning_at"]) + rng.randint(20, 150)
    st["next_drop_at"] = datetime.combine(today, datetime.min.time(), ctx.tz).timestamp() + first * 60
    st.pop("night_done", None)
    plan_urgent(ctx)
    log(f"New day {st['day']}: " + ("quiet day, nothing planned" if quiet else
        f"{'weekend, ' if weekend else ''}morning at {st['morning_at']}, night from {st['night_at']}, "
        f"about {st['hours']}h of work"))


def night_stage(ctx, active=False):
    st = ctx.st
    stage = st.get("night_stage", 0) + 1
    if stage == 1:
        if active:
            log("Night: you were in Jira today, only sorting what has waited a while")
            phase_night_sort(ctx, limit=5, older=1)
        else:
            phase_night_sort(ctx, lazy=st.get("lazy"))
    elif stage == 2:  # on your Jira days, half the hours and only work already in progress
        phase_night_work(ctx, st.get("hours", 3.0) * (0.5 if active else 1), active)
    else:
        last = parse_day(st.get("last_sprint_day"))
        plan = last is None or (ctx.today() - last).days >= ctx.cfg["sprint_every_days"]
        phase_night_wrap(ctx, plan=plan and not active, active=active)
    st["night_stage"] = stage
    st["night_next"] = utcnow().timestamp() + 20 * 60


def tick(ctx):
    st, cfg = ctx.st, ctx.cfg
    if st.pop("night_done", False):  # state from an older version
        st["night_stage"] = 3
    if st.get("day") != ctx.day():
        while st.get("night_stage", 3) in (1, 2):  # last night ran past midnight, finish it first
            night_stage(ctx)
        new_day(ctx)
    now = ctx.now()
    ts = now.timestamp()
    minutes = now.hour * 60 + now.minute
    start_h, end_h = cfg["active_hours"]
    awake = start_h <= now.hour < end_h
    if not st.get("kickoff_done") and not st.get("last_sprint_day") and awake:
        phase_kickoff(ctx)
        st["next_drop_at"] = ts + rng.uniform(*cfg["drop_gap_hours"]) * 3600
        return
    u = st.get("urgent") or {}
    followup_due = u.get("stage") in ("working", "review") and ts >= u.get("next", 0)
    if st.get("quiet"):
        if followup_due:
            phase_urgent_followup(ctx)
        else:
            log("Quiet day, nothing to do.")
        return

    morning_due = not st.get("morning_done") and minutes >= hhmm(st["morning_at"])
    if morning_due and now.hour >= 14:  # runner was offline all morning, skip it today
        st["morning_done"], morning_due = True, False
    drop_due = False
    if awake:
        nxt = st.get("next_drop_at")
        if nxt is None or nxt > ts + 8 * 3600:
            st["next_drop_at"] = ts + rng.uniform(0.3, 1.5) * 3600
        else:
            drop_due = ts >= nxt
    urgent_due = u.get("stage") == "planned" and minutes >= hhmm(u["at"])
    if urgent_due and now.hour >= end_h:  # runner was offline, skip it today
        u["stage"], urgent_due = None, False
    edit_due = bool(st.get("edit_at")) and minutes >= hhmm(st["edit_at"])
    night_due = st.get("night_stage", 0) < 3 and minutes >= hhmm(st["night_at"]) and ts >= st.get("night_next", 0)
    if not (morning_due or drop_due or urgent_due or followup_due or edit_due or night_due):
        nd = st.get("next_drop_at")
        when = datetime.fromtimestamp(nd, ctx.tz).strftime("%H:%M") if nd else "-"
        log(f"Nothing due. Next drop around {when}, morning {st['morning_at']}, night {st['night_at']}.")
        return

    jitter = float(cfg.get("jitter_minutes") or 0)
    if jitter > 0:
        wait = rng.uniform(0, jitter) * 60
        log(f"Waiting {wait / 60:.0f} min so the timing looks natural")
        SLEEP(wait)
    active = human_active(ctx)
    if morning_due:
        if not active:
            phase_morning(ctx)
        st["morning_done"] = True
    if drop_due and night_due:  # not while sorting the inbox, come back a bit later
        st["next_drop_at"] = ts + rng.uniform(30, 50) * 60
    elif drop_due:
        gap = cfg["weekend_drop_gap_hours"] if st.get("weekend") else cfg["drop_gap_hours"]
        if active:
            log("Drop: skipped, you are in Jira today")
        elif st.get("weekend") and rng.random() < 0.35:
            log("Drop: skipped, lazy weekend")
        else:
            phase_drop(ctx)
        st["next_drop_at"] = ts + rng.uniform(*gap) * 3600
    if urgent_due:
        if active:
            u["stage"] = None
        else:
            phase_urgent(ctx)
    elif followup_due:
        phase_urgent_followup(ctx)
    if edit_due:
        if not active:
            phase_edits(ctx)
        st["edit_at"] = None
    if night_due:
        night_stage(ctx, active)


# ---------------------------------------------------------------- check / audit

def cmd_check(ctx, store):
    log(f"Account   : {ctx.me_name}")
    log(f"Project   : {ctx.key} ({ctx.project_name}), issue type '{ctx.issue_type}'")
    log("Statuses  :")
    for role, name in ctx.status.items():
        log(f"  {role:<14}{name or '-  (not found, phase will adapt)'}")
    log("Fields    :")
    for k, v in ctx.f.items():
        note = "  (not on your issue screen, skipped)" if v and v in ctx.unsettable else ""
        log(f"  {k:<14}{v or '-'}{note}")
    log(f"Components: {', '.join(ctx.components) or '-'}")
    log(f"Epics     : {len(ctx.epics)}")
    boards = ctx.j.get("/rest/agile/1.0/board", projectKeyOrId=ctx.key, type="scrum").get("values", [])
    names = ", ".join("{} (id {})".format(b["name"], b["id"]) for b in boards)
    log(f"Scrum boards: {names or 'none, sprints will be skipped'}")
    log(f"State store: {store.where}")
    log(f"Gemini    : {'on, ' + ctx.cfg['gemini_model'] if gemini_key() else 'off (optional)'}")
    missing = [r for r in ("backlog", "in_progress", "done") if not ctx.status.get(r)]
    log("Ready." if not missing else f"Warning: could not map {', '.join(missing)}. Edit 'statuses' in config.json.")
    log(f"Available statuses for {ctx.issue_type}: {', '.join(ctx.status_names)}")


def cmd_audit(ctx):
    open_ = ctx.j.search(f"{ctx.base()} AND statusCategory != Done ORDER BY updated DESC", ["summary"], 25)
    log(f"PROJECT {ctx.key} '{ctx.project_name}'")
    log("EPICS: " + "; ".join(e["summary"] for e in ctx.epics[:40]))
    log("COMPONENTS: " + "; ".join(ctx.components))
    log("RECENT OPEN TASKS: " + "; ".join(i["fields"]["summary"] for i in open_))


# ---------------------------------------------------------------- main

COMMANDS = ["tick", "check", "audit", "kickoff", "urgent", "inbox", "setup", "drop", "morning", "night",
            "sprint", "edit", "refill"]


def run(ctx, command, count=None, store=None):
    st, cfg = ctx.st, ctx.cfg
    if command == "check":
        cmd_check(ctx, store)
    elif command == "audit":
        cmd_audit(ctx)
    elif command == "tick":
        tick(ctx)
    elif command == "kickoff":
        phase_kickoff(ctx)
    elif command == "urgent":
        phase_urgent(ctx)
    elif command == "inbox":
        phase_inbox(ctx, os.environ.get("ISSUE_TITLE", ""), os.environ.get("ISSUE_BODY", ""))
    elif command == "setup":
        cmd_setup(ctx, os.environ.get("ISSUE_BODY", ""))
    elif command == "drop":
        phase_drop(ctx, count)
    elif command == "morning":
        phase_morning(ctx)
    elif command == "night":  # all three night runs back to back
        phase_night_sort(ctx)
        phase_night_work(ctx, st.get("hours") or 3.0)
        phase_night_wrap(ctx, plan=True)
    elif command == "sprint":
        phase_sprint(ctx)
    elif command == "edit":
        phase_edits(ctx)
    elif command == "refill":
        recent = ctx.j.search(f"project = {jq(ctx.key)} AND created >= -21d", ["summary"], 300)
        extra = llm_tasks(ctx.personal, [i["fields"]["summary"] for i in recent], cfg["gemini_model"])
        st["extra"] = (st.get("extra", []) + [[t["t"], t["s"], t["a"]] for t in extra])[-80:]
        log(f"Added {len(extra)} ideas." if extra else "No ideas added. Check GEMINI_API_KEY.")


def alert(msg):
    """On GitHub, ALERT_FILE turns into one 'Autopilot needs attention' issue instead of a
    failed-run email every half hour. Locally there is no ALERT_FILE, so it just stops."""
    path = os.environ.get("ALERT_FILE")
    if not path:
        sys.exit(msg)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    log(msg)


def login_help(e):
    if e.code in (401, 403):
        return (f"Jira login failed ({e.code}). Your API token has probably expired or was revoked.\n\n"
                "1. Make a new one at https://id.atlassian.com/manage-profile/security/api-tokens\n"
                "2. Repo Settings → Secrets and variables → Actions → JIRA_TOKEN → Update, paste it.\n\n"
                "This issue closes itself on the next run that works.")
    return f"Could not reach Jira: {e}\n\nCheck JIRA_SITE and JIRA_EMAIL. This issue closes itself on the next run that works."


def main():
    ap = argparse.ArgumentParser(description="Keep a Jira project active on a human-looking schedule.")
    ap.add_argument("command", nargs="?", default="tick", choices=COMMANDS)
    ap.add_argument("--dry-run", action="store_true", help="show actions without changing Jira")
    ap.add_argument("--count", type=int, help="items to add with 'drop'")
    args = ap.parse_args()

    load_env_file(os.path.join(HERE, ".env"))
    cfg = deep_merge(DEFAULTS, load_json(os.path.join(HERE, "config.json"), {}))
    cfg["project_key"] = os.environ.get("JIRA_PROJECT") or cfg["project_key"]
    site, email, token = os.environ.get("JIRA_SITE"), os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_TOKEN")
    if not (site and email and token and cfg["project_key"]):
        return alert("Missing JIRA_SITE, JIRA_EMAIL, JIRA_TOKEN or JIRA_PROJECT. See README.md, step 3.")

    jira = Jira(site, email, token, dry=args.dry_run)
    try:
        me = jira.get("/rest/api/3/myself")
    except JiraError as e:
        return alert(login_help(e))
    store = Store(jira, me["accountId"], args.dry_run)
    st = store.load()
    generic = load_json(os.path.join(HERE, "generic_tasks.json"), {})
    personal = load_json(os.path.join(HERE, "tasks.json"), {})
    personal["context"] = load_context(os.path.join(HERE, cfg["context_file"]))
    try:
        ctx = Ctx(cfg, jira, st, generic, personal)
    except JiraError as e:
        return alert(f"Could not read project {cfg['project_key']}: {e}\n\nCheck the JIRA_PROJECT variable.")
    if args.dry_run:
        log("(dry run, nothing will change)")

    problem = None
    try:
        run(ctx, args.command, args.count, store)
    except JiraError as e:
        problem = f"Jira error: {e}"
    except Exception:  # a bug should raise one alert after a few runs, not a failed-run email each time
        problem = "Unexpected error:\n" + traceback.format_exc()
        if not os.environ.get("ALERT_FILE"):
            raise
    finally:
        st["errors_in_row"] = st.get("errors_in_row", 0) + 1 if problem else 0
        store.save(st)
    if problem:
        log(problem)
        if st["errors_in_row"] >= 3:
            alert(f"The last {st['errors_in_row']} runs hit an error. The most recent one:\n\n{problem}")
    elif os.environ.get("HEALTHY_FILE"):
        open(os.environ["HEALTHY_FILE"], "w").close()


if __name__ == "__main__":
    main()
