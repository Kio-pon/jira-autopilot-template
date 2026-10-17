#!/usr/bin/env python3
"""
Jira Autopilot
Keeps a Jira project active on a human-looking daily rhythm:
  - drops new tasks into the inbox at random 2 to 3 hour gaps
  - morning: reviews deferred items and starts a few tasks
  - night: triages the inbox, fills fields, moves work along, finishes some
  - every few days: plans, starts and closes 1-week sprints
Needs only Python 3.9+. Uses no AI tokens day to day.

Commands:
  python autopilot.py check     verify credentials and show how your project maps
  python autopilot.py audit     compact summary to paste into a chatbot (see SETUP_PROMPT.md)
  python autopilot.py tick      what the scheduler runs; does whatever is due right now
  python autopilot.py drop | morning | night | sprint    force one phase now
  python autopilot.py refill    ask your LLM (optional) for fresh task ideas
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
    "drop_gap_hours": [2.0, 3.0],
    "drop_count_weights": [70, 22, 8],
    "quick_ratio": 0.4,
    "everyday_ratio": 0.55,
    "morning_window": ["08:00", "09:30"],
    "night_window": ["22:00", "23:30"],
    "sprint_every_days": 2,
    "sprint_length_days": 7,
    "sprint_size": [6, 12],
    "sprint_name_prefix": "",
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

COMMENTS = {
    "start": ["Started on this.", "Got going on this before class.", "Picked this up today.",
              "Starting now, should not take long.", "Made a start, more tomorrow."],
    "progress": ["About halfway there.", "Decent progress, finishing tomorrow.", "Got stuck for a bit, back on track now.",
                 "Most of it is done.", "Slow going but moving."],
    "done": ["Done.", "Finished this one.", "Wrapped up.", "Sorted.", "Done, took longer than expected.", "Finally done."],
    "push": ["Ran out of time, doing this tomorrow.", "Pushing this a couple of days.", "Could not get to it today.",
             "Busy day, moving this.", "Will pick this up after the weekend."],
    "review": ["Done, just want to double check it.", "Finished, one more look before closing."],
}
DESC = {
    "q": ["Small one, just get it done.", "Should take five minutes.", "Keep forgetting this one.",
          "Do it between classes.", "Quick task, do not overthink it.", "Needs doing today or tomorrow."],
    "b": ["Needs a proper sit-down session.", "Block an hour or two for this.", "Break it into parts if it drags.",
          "Want this done before the weekend.", "Been putting this off, time to start.", "Do this on a free evening."],
}

rng = random.Random(os.environ.get("AUTOPILOT_SEED"))


# ---------------------------------------------------------------- helpers

def log(msg=""):
    print(msg, flush=True)


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
    def __init__(self, jira, account_id, dry):
        self.j, self.acc, self.dry = jira, account_id, dry
        self.path = os.path.join(HERE, "state.json")
        self.where = "jira"

    def load(self):
        try:
            r = self.j.get(f"/rest/api/3/user/properties/{STATE_PROP}", accountId=self.acc)
            return r.get("value") or {}
        except JiraError as e:
            if e.code == 404:
                return load_json(self.path, {})
            self.where = "file"
            log(f"  note: could not read state from Jira ({e.code}), using state.json")
            return load_json(self.path, {})

    def save(self, st):
        meta = st.get("meta", {})
        while len(json.dumps(st)) > 30000 and meta:
            meta.pop(next(iter(meta)))
        if self.dry:
            return
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
        self.templated = []
        for c in self.courses:
            short = c.get("short") or c["name"]
            for tpl in generic.get("course_templates", []):
                self.templated.append({"t": tpl[0].format(c=short), "s": tpl[1], "a": c["name"], "d": ""})
        for p in self.projects:
            short = p.get("short") or p["name"]
            for tpl in generic.get("project_templates", []):
                self.templated.append({"t": tpl[0].format(p=short), "s": tpl[1], "a": p["name"], "d": ""})

    def kind(self, area):
        n = norm(area)
        if any(norm(c["name"]) == n for c in self.courses):
            return "course"
        if any(norm(p["name"]) == n for p in self.projects):
            return "project"
        return n if n else "self"

    def fresh(self, bucket, used):
        return [t for t in bucket if t["t"] and norm(t["t"]) not in used]

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


def llm_tasks(personal, recent_titles, n=25):
    base, key, model = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY"),
                        os.environ.get("LLM_MODEL"))
    if not (base and key and model):
        return []
    courses = ", ".join((c if isinstance(c, str) else c.get("name", "")) for c in personal.get("courses", []))
    projects = ", ".join((p if isinstance(p, str) else p.get("name", "")) for p in personal.get("projects", []))
    prompt = (
        f"Generate {n} new to-do items for this person's personal Jira board.\n"
        f"Person: {personal.get('profile', 'a university student')}\n"
        f"Courses: {courses or 'none'}\nProjects: {projects or 'none'}\n"
        f"Avoid these recent titles: {'; '.join(list(recent_titles)[:40])}\n"
        "Mix about half everyday life tasks and half course or project tasks. About 40% quick (under 10 min).\n"
        'Return ONLY a JSON array of [title, "q" or "b", area] where area is a course name, a project name, '
        "or one of chores, errands, tech, social, family, health, self, admin, finance. Titles 3 to 9 words."
    )
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.9}).encode()
    request = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
        m = re.search(r"\[[\s\S]*\]", content)
        items = json.loads(m.group(0)) if m else []
        return [mk_task(t) for t in items if t and (isinstance(t, (list, dict)))]
    except Exception as e:  # never let an optional LLM break the run
        log(f"  LLM refill failed: {e}")
        return []


# ---------------------------------------------------------------- project context

class Ctx:
    def __init__(self, cfg, jira, st, generic, personal):
        self.cfg, self.j, self.st = cfg, jira, st
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

    # time
    def now(self):
        return datetime.now(self.tz)

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

    def find(self, extra, order="ORDER BY duedate ASC", limit=100):
        issues = self.j.search(f"{self.base()} AND {extra} {order}", self.search_fields(), limit)
        if self.cfg["touch_existing_tasks"]:
            return issues
        mine = self.st.get("meta", {})
        return [i for i in issues if i["key"] in mine]

    # writes
    def move(self, key, role):
        target = self.status.get(role)
        if not target:
            return False
        trs = self.j.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        for t in trs:
            if norm(t["to"]["name"]) == norm(target):
                self.j.post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                log(f"  {key} -> {target}")
                return True
        log(f"  {key}: no direct transition to {target}, skipped")
        return False

    def finish(self, key):
        if self.move(key, "done"):
            return True
        return self.move(key, "in_progress") and self.move(key, "done")

    def edit(self, key, fields):
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        try:
            self.j.put(f"/rest/api/3/issue/{key}", {"fields": fields})
        except JiraError as e:
            if e.code != 400 or len(fields) == 1:
                log(f"  {key}: could not set {', '.join(fields)} ({e.code})")
                return
            for k, v in fields.items():
                try:
                    self.j.put(f"/rest/api/3/issue/{key}", {"fields": {k: v}})
                except JiraError:
                    log(f"  {key}: skipped field {k}")

    def comment(self, key, kind):
        text = rng.choice(COMMENTS[kind])
        self.j.post(f"/rest/api/3/issue/{key}/comment", {"body": adf(text)})
        log(f"  {key}: commented '{text}'")

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
        kind = self.pool.kind(area)
        name = self.cfg["area_epics"].get(kind) if kind not in ("course", "project") else area
        if not name:
            return None
        want = tokens(name)
        best, score = None, 0.0
        for e in self.epics:
            have = tokens(e["summary"])
            if want and have:
                s = len(want & have) / min(len(want), len(have))
                if s > score:
                    best, score = e, s
        if best and score >= 0.5:
            return best["key"]
        if self.epic_budget > 0:
            self.epic_budget -= 1
            r = self.j.post("/rest/api/3/issue", {"fields": {"project": {"key": self.key},
                                                             "issuetype": {"name": "Epic"}, "summary": name}})
            log(f"  created epic '{name}' {r.get('key', '')}")
            if r.get("key"):
                self.epics.append({"key": r["key"], "summary": name})
                return r["key"]
        return None

    def component_for(self, area):
        kind = self.pool.kind(area)
        have = {norm(c): c for c in self.components}
        for c in self.cfg["area_components"].get(kind, []):
            if norm(c) in have:
                return [{"name": have[norm(c)]}]
        return None

    def info(self, issue):
        m = self.st.get("meta", {}).get(issue["key"])
        if m:
            return m
        f = issue["fields"]
        sp = f.get(self.f["story_points"]) if self.f.get("story_points") else None
        size = "q" if (isinstance(sp, (int, float)) and sp <= 1) else "b"
        words = norm(f.get("summary"))
        area = "self"
        for c in self.pool.courses + self.pool.projects:
            if tokens(c.get("short") or c["name"]) & set(words.split()):
                area = c["name"]
                break
        else:
            if any(w in words for w in ("quiz", "assignment", "lab", "lecture", "exam", "class")):
                area = "course" if not self.pool.courses else self.pool.courses[0]["name"]
        return {"a": area, "s": size, "d": None}

    def remember(self, key, data):
        self.st.setdefault("meta", {})[key] = data


# ---------------------------------------------------------------- phases

def phase_drop(ctx, count=None):
    log("Drop: adding inbox items")
    recent = ctx.j.search(f"project = {jq(ctx.key)} AND created >= -21d", ["summary"], 300)
    used = {norm(i["fields"]["summary"]) for i in recent}
    if len(ctx.pool.fresh(ctx.pool.specific, used)) < 10 and os.environ.get("LLM_API_KEY"):
        extra = llm_tasks(ctx.pool.personal, [i["fields"]["summary"] for i in recent])
        if extra:
            ctx.st["extra"] = (ctx.st.get("extra", []) + [[t["t"], t["s"], t["a"]] for t in extra])[-80:]
            ctx.pool.specific += extra
            log(f"  refilled {len(extra)} ideas from LLM")
    n = count or weighted([1, 2, 3], ctx.cfg["drop_count_weights"])
    picks = ctx.pool.pick(n, used, ctx.cfg["everyday_ratio"], ctx.cfg["quick_ratio"])
    for t in picks:
        due = ctx.day(rng.randint(0, 2)) if t["s"] == "q" else ctx.day(rng.randint(2, 9))
        desc = t["d"] or rng.choice(DESC[t["s"]])
        if t["s"] == "b" and rng.random() < 0.4:
            d = parse_day(due)
            desc += f"\nDeadline: {d.strftime('%A')} {d.day} {d.strftime('%B')}"
        fields = {"project": {"key": ctx.key}, "issuetype": {"name": ctx.issue_type},
                  "summary": t["t"], "description": adf(desc)}
        if ctx.cfg["label"]:
            fields["labels"] = [ctx.cfg["label"]]
        r = ctx.j.post("/rest/api/3/issue", {"fields": fields})
        key = r.get("key")
        log(f"  + {key or '(new)'} {t['t']}")
        if not key:
            continue
        ctx.remember(key, {"a": t["a"], "s": t["s"], "d": due})
        inbox = ctx.status.get("inbox")
        if inbox:
            cur = ctx.j.get(f"/rest/api/3/issue/{key}", fields="status")["fields"]["status"]["name"]
            if norm(cur) != norm(inbox):
                ctx.move(key, "inbox")
    return len(picks)


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
            if rng.random() < 0.7:
                ctx.move(i["key"], "process_today") or ctx.move(i["key"], "backlog")
            elif ctx.f.get("defer_until"):
                ctx.edit(i["key"], {ctx.f["defer_until"]: ctx.day(rng.randint(2, 6))})
                log(f"  {i['key']}: deferred again")
    pool = ctx.sjql("backlog", "plan")
    if pool:
        cands = ctx.find(pool)
        tomorrow = ctx.day(1)
        hot = [i for i in cands if (i["fields"].get("duedate") or "9999") <= tomorrow or ctx.is_urgent(i)]
        picks = hot[:rng.randint(1, 3)] or (rng.sample(cands, 1) if cands else [])
        for i in picks:
            ctx.move(i["key"], "in_progress")
    if S.get("in_progress") and rng.random() < 0.34:
        wip = ctx.find(f"status = {jq(S['in_progress'])}")
        if wip:
            ctx.comment(rng.choice(wip)["key"], "start")


def phase_night(ctx):
    log("Night: triage, processing, progress review")
    S, f = ctx.status, ctx.f
    process_keys = []

    # 1. inbox triage
    if S.get("inbox"):
        for i in ctx.find(f"status = {jq(S['inbox'])}", order="ORDER BY created ASC"):
            k, inf = i["key"], ctx.info(i)
            if not i["fields"].get("parent"):
                epic = ctx.epic_for(inf["a"])
                if epic:
                    ctx.edit(k, {"parent": {"key": epic}})
            if inf["s"] == "q" and rng.random() < 0.35:
                ctx.finish(k)
            elif S.get("deferred") and rng.random() < 0.25:
                if f.get("defer_until"):
                    ctx.edit(k, {f["defer_until"]: ctx.day(rng.randint(1, 5))})
                ctx.move(k, "deferred")
            elif ctx.move(k, "process_today"):
                process_keys.append(k)
            elif ctx.move(k, "backlog"):
                process_keys.append(k)

    # 2. process
    todo = []
    if S.get("process_today"):
        todo += ctx.find(f"status = {jq(S['process_today'])}", order="ORDER BY created ASC")
    if not S.get("inbox") and S.get("backlog"):
        todo += ctx.find(f"status = {jq(S['backlog'])} AND created >= -2d AND duedate is EMPTY")
    seen = {i["key"] for i in todo}
    for k in process_keys:
        if k not in seen:
            issue = ctx.j.get(f"/rest/api/3/issue/{k}", fields=",".join(ctx.search_fields()))
            todo.append(issue if issue.get("key") else {"key": k, "fields": {}})
    for i in todo:
        k, fl, inf = i["key"], i.get("fields") or {}, ctx.info(i)
        due = fl.get("duedate") or inf.get("d") or \
            (ctx.day(rng.randint(0, 2)) if inf["s"] == "q" else ctx.day(rng.randint(2, 9)))
        urgent = due <= ctx.day(2)
        quick = inf["s"] == "q"
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
            fields[f["story_points"]] = 1 if quick else rng.choice([2, 3, 3, 5, 5, 8])
        if f.get("deep_shallow"):
            deep = (not quick) and rng.random() < 0.75
            fields[f["deep_shallow"]] = ctx.option(k, "deep_shallow", ["deep"] if deep else ["shallow"])
        if f.get("task_size"):
            fields[f["task_size"]] = ctx.option(k, "task_size",
                                                ["small", "quick", "<"] if quick else ["big", "large", ">"])
        ctx.edit(k, fields)
        log(f"  {k}: fields filled (due {due})")
        title = norm(fl.get("summary"))
        if any(w in title for w in PLAN_WORDS) and S.get("plan"):
            ctx.move(k, "plan")
        elif (not quick) and S.get("later") and rng.random() < 0.08:
            ctx.move(k, "later")
        elif S.get("process_today"):
            ctx.move(k, "backlog")

    # 3. progress review
    scope = ctx.sjql("review", "in_progress", "plan", "backlog")
    if not scope:
        return
    open_ = ctx.find(scope)
    budget = min(rng.randint(3, 7), max(0, len(open_) - 3))
    today, changed, touched = ctx.day(), 0, []

    def st_of(i):
        return norm(i["fields"]["status"]["name"])

    def is_(i, role):
        return S.get(role) and st_of(i) == norm(S[role])

    queue = [i for i in open_ if is_(i, "review")] + [i for i in open_ if is_(i, "in_progress")] + \
            [i for i in open_ if (is_(i, "backlog") or is_(i, "plan")) and
             (i["fields"].get("duedate") or "9999") <= today]
    stale_cut = (ctx.now() - timedelta(days=21)).isoformat()
    queue += [i for i in open_ if (is_(i, "backlog") or is_(i, "plan")) and
              (i["fields"].get("updated") or "9999") < stale_cut and i not in queue]
    for i in queue:
        if changed >= budget:
            break
        k = i["key"]
        r = rng.random()
        if is_(i, "review"):
            if r < 0.8 and ctx.finish(k):
                changed += 1
                touched.append((k, "done"))
        elif is_(i, "in_progress"):
            if r < 0.4 and (ctx.move(k, "review") if S.get("review") else ctx.finish(k)):
                changed += 1
                touched.append((k, "review" if S.get("review") else "done"))
            elif r < 0.65 and ctx.finish(k):
                changed += 1
                touched.append((k, "done"))
            else:
                touched.append((k, "progress"))
        elif (i["fields"].get("duedate") or "9999") <= today:
            if r < 0.65 and ctx.finish(k):
                changed += 1
                touched.append((k, "done"))
            elif str(i["fields"].get("created", ""))[:10] == today:
                continue  # just created, leave it for tomorrow
            else:
                ctx.edit(k, {"duedate": ctx.day(rng.randint(1, 3))})
                ctx.comment(k, "push")
                changed += 1
        else:
            if r < 0.5 and ctx.finish(k):
                changed += 1
                touched.append((k, "done"))
            elif S.get("later") and ctx.move(k, "later"):
                changed += 1
    for k, kind in rng.sample(touched, min(len(touched), rng.randint(1, 2))):
        ctx.comment(k, kind)
    log(f"  progress changes: {changed}")


def phase_sprint(ctx):
    log("Sprint: planning and upkeep")
    b = ctx.cfg.get("board_id")
    if not b:
        boards = ctx.j.get("/rest/agile/1.0/board", projectKeyOrId=ctx.key, type="scrum").get("values", [])
        if not boards:
            log("  no scrum board in this project, skipping sprints")
            return
        b = boards[0]["id"]
        for bd in boards:
            if ctx.j.get(f"/rest/agile/1.0/board/{bd['id']}/sprint", maxResults=1).get("values"):
                b = bd["id"]
                break
    active = ctx.j.get(f"/rest/agile/1.0/board/{b}/sprint", state="active").get("values", [])
    now = ctx.now()
    if active:
        s = active[0]
        end = parse_dt(s.get("endDate"))
        if end and end - now <= timedelta(hours=24):
            ctx.j.post(f"/rest/agile/1.0/sprint/{s['id']}", {"state": "closed"})
            log(f"  closed sprint '{s['name']}'")
            start_sprint(ctx, b, carry=s["id"])
        else:
            keep_sprint(ctx, s)
    elif not start_sprint(ctx, b):
        return  # try again tomorrow night
    ctx.st["last_sprint_day"] = ctx.day()


def start_sprint(ctx, board, carry=None):
    scope = ctx.sjql("backlog", "plan", "in_progress", "review")
    carried = ctx.find(f"sprint = {carry} AND statusCategory != Done") if carry else []
    others = []
    if scope:
        taken = {c["key"] for c in carried}
        others = [i for i in ctx.find(f"{scope} AND statusCategory != Done") if i["key"] not in taken]
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
    estimates = {}
    for i in ordered:
        if len(chosen) >= target or points >= 30:
            break
        est = i["fields"].get(sp_field) if sp_field else None
        if sp_field and not est:
            est = rng.choice([1, 2, 3, 5])
            estimates[i["key"]] = est
        points += est or 0
        chosen.append(i["key"])
    for k, v in estimates.items():
        ctx.edit(k, {sp_field: v})
    ctx.j.post(f"/rest/agile/1.0/sprint/{sp['id']}/issue", {"issues": chosen})
    themes = []
    for i in ordered:
        if i["key"] in chosen and i["fields"].get("parent"):
            name = i["fields"]["parent"].get("fields", {}).get("summary")
            if name and name not in themes:
                themes.append(name)
    goal = (rng.choice(["Get through {0} and keep {1} moving", "Focus on {0}, then {1}", "Clear {0} and push {1}"])
            .format(themes[0], themes[1]) if len(themes) >= 2 else
            f"Make solid progress on {themes[0]}" if themes else "Clear the most urgent work this week")
    start = ctx.now()
    end = (start + timedelta(days=ctx.cfg["sprint_length_days"])).replace(hour=18, minute=0, second=0, microsecond=0)
    ctx.j.post(f"/rest/agile/1.0/sprint/{sp['id']}", {
        "name": sp["name"], "state": "active", "goal": goal,
        "startDate": start.isoformat(timespec="seconds"), "endDate": end.isoformat(timespec="seconds")})
    log(f"  started '{sp['name']}' with {len(chosen)} issues, {points} points. Goal: {goal}")
    return True


def keep_sprint(ctx, s):
    inside = ctx.find(f"sprint = {s['id']}")
    end = (parse_dt(s.get("endDate")) or ctx.now()).date().isoformat()
    scope = ctx.sjql("backlog", "plan")
    if scope:
        outside = ctx.find(f"{scope} AND (sprint is EMPTY OR sprint not in openSprints())")
        hot = [i for i in outside if ctx.is_urgent(i) or (i["fields"].get("duedate") or "9999") <= end]
        add = [i["key"] for i in hot[:rng.randint(1, 3)]]
        if add:
            ctx.j.post(f"/rest/agile/1.0/sprint/{s['id']}/issue", {"issues": add})
            log(f"  added {', '.join(add)} to '{s['name']}'")
    S = ctx.status
    if S.get("in_progress") and not any(norm(i["fields"]["status"]["name"]) == norm(S["in_progress"])
                                        for i in inside):
        waiting = [i for i in inside if i["fields"]["status"]["name"] in (S.get("backlog"), S.get("plan"))]
        for i in waiting[:rng.randint(1, 2)]:
            ctx.move(i["key"], "in_progress")
    log(f"  sprint '{s['name']}' mid-way, {len(inside)} issues")


# ---------------------------------------------------------------- scheduler

def rand_time(window):
    a, b = hhmm(window[0]), hhmm(window[1])
    m = rng.randint(a, max(a, b))
    return f"{m // 60:02d}:{m % 60:02d}"


def tick(ctx):
    st, cfg = ctx.st, ctx.cfg
    now = ctx.now()
    today = ctx.day()
    if st.get("day") != today:
        st.update(day=today, morning_at=rand_time(cfg["morning_window"]),
                  night_at=rand_time(cfg["night_window"]), morning_done=False, night_done=False)
        log(f"New day {today}: morning at {st['morning_at']}, night at {st['night_at']}")
    minutes = now.hour * 60 + now.minute
    ts = now.timestamp()
    start_h, end_h = cfg["active_hours"]
    morning_due = not st["morning_done"] and minutes >= hhmm(st["morning_at"])
    if morning_due and now.hour >= 14:  # runner was offline all morning, skip it today
        st["morning_done"], morning_due = True, False
    drop_due = False
    if start_h <= now.hour < end_h:
        nxt = st.get("next_drop_at")
        if nxt is None or nxt > ts + 6 * 3600:
            st["next_drop_at"] = ts + rng.uniform(0.3, 1.5) * 3600
        else:
            drop_due = ts >= nxt
    night_due = not st["night_done"] and minutes >= hhmm(st["night_at"])
    did = morning_due or drop_due or night_due

    jitter = float(cfg.get("jitter_minutes") or 0)
    if did and jitter > 0:
        wait = rng.uniform(0, jitter) * 60
        log(f"Waiting {wait / 60:.0f} min so the timing looks natural")
        time.sleep(wait)

    if morning_due:
        phase_morning(ctx)
        st["morning_done"] = True
    if drop_due:
        phase_drop(ctx)
        st["next_drop_at"] = ts + rng.uniform(*cfg["drop_gap_hours"]) * 3600
    if night_due:
        phase_night(ctx)
        last = parse_day(st.get("last_sprint_day"))
        if last is None or (ctx.today() - last).days >= cfg["sprint_every_days"]:
            phase_sprint(ctx)
        st["night_done"] = True
    if not did:
        nd = st.get("next_drop_at")
        when = datetime.fromtimestamp(nd, ctx.tz).strftime("%H:%M") if nd else "-"
        log(f"Nothing due. Next drop around {when}, morning {st['morning_at']}, night {st['night_at']}.")


# ---------------------------------------------------------------- check / audit

def cmd_check(ctx, store):
    log(f"Account   : {ctx.me_name}")
    log(f"Project   : {ctx.key} ({ctx.project_name}), issue type '{ctx.issue_type}'")
    log("Statuses  :")
    for role, name in ctx.status.items():
        log(f"  {role:<14}{name or '-  (not found, phase will adapt)'}")
    log("Fields    :")
    for k, v in ctx.f.items():
        log(f"  {k:<14}{v or '-'}")
    log(f"Components: {', '.join(ctx.components) or '-'}")
    log(f"Epics     : {len(ctx.epics)}")
    boards = ctx.j.get("/rest/agile/1.0/board", projectKeyOrId=ctx.key, type="scrum").get("values", [])
    names = ", ".join("{} (id {})".format(b["name"], b["id"]) for b in boards)
    log(f"Scrum boards: {names or 'none, sprints will be skipped'}")
    log(f"State store: {store.where}")
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

def main():
    ap = argparse.ArgumentParser(description="Keep a Jira project active on a human-looking schedule.")
    ap.add_argument("command", nargs="?", default="tick",
                    choices=["tick", "check", "audit", "drop", "morning", "night", "sprint", "refill"])
    ap.add_argument("--dry-run", action="store_true", help="show actions without changing Jira")
    ap.add_argument("--count", type=int, help="items to add with 'drop'")
    args = ap.parse_args()

    load_env_file(os.path.join(HERE, ".env"))
    cfg = deep_merge(DEFAULTS, load_json(os.path.join(HERE, "config.json"), {}))
    cfg["project_key"] = os.environ.get("JIRA_PROJECT") or cfg["project_key"]
    site, email, token = os.environ.get("JIRA_SITE"), os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_TOKEN")
    if not (site and email and token and cfg["project_key"]):
        sys.exit("Missing JIRA_SITE, JIRA_EMAIL, JIRA_TOKEN or JIRA_PROJECT. See README.md.")

    jira = Jira(site, email, token, dry=args.dry_run)
    try:
        me = jira.get("/rest/api/3/myself")
    except JiraError as e:
        sys.exit(f"Could not log in to Jira: {e}")
    store = Store(jira, me["accountId"], args.dry_run)
    st = store.load()
    generic = load_json(os.path.join(HERE, "generic_tasks.json"), {})
    personal = load_json(os.path.join(HERE, "tasks.json"), {})
    try:
        ctx = Ctx(cfg, jira, st, generic, personal)
    except JiraError as e:
        sys.exit(f"Could not read project {cfg['project_key']}: {e}")
    if args.dry_run:
        log("(dry run, nothing will change)")

    if args.command == "check":
        return cmd_check(ctx, store)
    if args.command == "audit":
        return cmd_audit(ctx)
    try:
        if args.command == "tick":
            tick(ctx)
        elif args.command == "drop":
            phase_drop(ctx, args.count)
        elif args.command == "morning":
            phase_morning(ctx)
        elif args.command == "night":
            phase_night(ctx)
        elif args.command == "sprint":
            phase_sprint(ctx)
        elif args.command == "refill":
            recent = jira.search(f"project = {jq(ctx.key)} AND created >= -21d", ["summary"], 300)
            extra = llm_tasks(personal, [i["fields"]["summary"] for i in recent])
            st["extra"] = (st.get("extra", []) + [[t["t"], t["s"], t["a"]] for t in extra])[-80:]
            log(f"Added {len(extra)} ideas." if extra else "No ideas added. Check LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.")
    except JiraError as e:
        log(f"Jira error: {e}")
    finally:
        store.save(st)


if __name__ == "__main__":
    main()
