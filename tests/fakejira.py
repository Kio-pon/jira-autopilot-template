"""An in-memory Jira for tests.

It answers the REST calls autopilot.py makes, evaluates the JQL it sends, and enforces the
rules a real SEPM course board enforces (an epic before a task can leave the inbox, a
component before it can enter the semester backlog, Story Points not on the screen).
Every change is logged with its time and whether the bot or a "human" made it, so the
simulator can check what the board history would look like.
"""
import json
import re
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=5))


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


S07, S08, S06 = "07 - BACKLOG UNPROCESSED", "08 - DEFER BACKLOG UNPROCESSED", "06 - BACKLOG PROCESS TODAY"
S04, S05, S09 = "04 - BACKLOG THIS SEMESTER", "05 - BACKLOG NOT THIS SEMESTER", "09 - PLAN"
S02, S03, S01 = "02 - IN PROGRESS", "03 - UNDER REVIEW", "01 - DONE"

PROJECTS = {
    "sepm": {
        "statuses": [S09, S02, S07, S01, S05, S06, S08, S04, S03],
        "start": S07,
        "done": {S01}, "doing": {S02, S03},
        "flows": {S07: [S06, S08, S01], S08: [S06, S04, S07], S06: [S04, S05, S09, S08, S02, S01],
                  S04: [S02, S09, S05, S01, S06], S05: [S04, S06], S09: [S02, S04, S01],
                  S02: [S03, S01, S04], S03: [S01, S02], S01: [S02, S04]},
        "fields": [("customfield_10042", "Story Points"), ("customfield_10046", "Defer Until"),
                   ("customfield_10044", "Deep/Shallow? "), ("customfield_10045", "Task Size (<10min/>10min) "),
                   ("customfield_10020", "Sprint"), ("customfield_10016", "Story point estimate")],
        "unsettable": {"customfield_10042"},
        "options": {"customfield_10044": ["Deep", "Shallow"], "customfield_10045": ["Small", "Big"]},
        "priorities": ["TODAY", "NOT TODAY"], "default_priority": "NOT TODAY",
        "components": ["Admin & Errands", "Career", "Coursework", "Devices & Tech", "Faith", "Health",
                       "Robocat (own project)", "Startup & Side Projects"],
        "epics": ["MIDAS", "SEPM", "DBMS", "DCN", "Daily practice", "Body upkeep", "Money", "Documents",
                  "Job search", "Robocat build", "Voltrix", "Operating Systems"],
        "needs_parent_to_leave": {S07}, "needs_component_to_enter": {S04}, "epic_needs_component": True,
    },
    "plain": {
        "statuses": ["To Do", "In Progress", "Done"],
        "start": "To Do",
        "done": {"Done"}, "doing": {"In Progress"},
        "flows": {"To Do": ["In Progress", "Done"], "In Progress": ["To Do", "Done"], "Done": ["To Do", "In Progress"]},
        "fields": [("customfield_10016", "Story point estimate"), ("customfield_10020", "Sprint")],
        "unsettable": set(), "options": {},
        "priorities": ["Highest", "High", "Medium", "Low"], "default_priority": "Medium",
        "components": [], "epics": [],
        "needs_parent_to_leave": set(), "needs_component_to_enter": set(), "epic_needs_component": False,
    },
}
DATE_FIELDS = {"customfield_10046"}


def jira_time(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S.000%z")


# ---------------------------------------------------------------- JQL subset

TOK = re.compile(r'\s*(?:(?P<s>"(?:[^"\\]|\\.)*")|(?P<op>!=|<=|>=|=|<|>|~)|(?P<p>[(),])|(?P<w>[^\s(),"=!<>~]+))')


def tokenize(q):
    out, pos = [], 0
    q = q.strip()
    while pos < len(q):
        m = TOK.match(q, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read JQL at: {q[pos:]}")
        pos = m.end()
        if m.group("s"):
            out.append(("s", json.loads(m.group("s"))))
        elif m.group("op"):
            out.append(("op", m.group("op")))
        elif m.group("p"):
            out.append(("p", m.group("p")))
        elif m.group("w"):
            out.append(("w", m.group("w")))
    return out


class JQL:
    def __init__(self, q):
        self.t, self.i = tokenize(q), 0
        self.expr = self.or_() if self.t and not self.at_word("ORDER") else None
        self.order = []
        if self.at_word("ORDER"):
            self.i += 2
            while self.i < len(self.t):
                field = self.next()[1]
                desc = False
                if self.at_word("ASC") or self.at_word("DESC"):
                    desc = self.next()[1].upper() == "DESC"
                self.order.append((field, desc))
                if self.peek() == ("p", ","):
                    self.i += 1
        if self.i != len(self.t):
            raise ValueError(f"unread JQL: {self.t[self.i:]}")

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def at_word(self, w):
        k, v = self.peek()
        return k == "w" and v.upper() == w

    def or_(self):
        e = self.and_()
        while self.at_word("OR"):
            self.i += 1
            e = ("or", e, self.and_())
        return e

    def and_(self):
        e = self.not_()
        while self.at_word("AND"):
            self.i += 1
            e = ("and", e, self.not_())
        return e

    def not_(self):
        if self.at_word("NOT"):
            self.i += 1
            return ("not", self.not_())
        if self.peek() == ("p", "("):
            self.i += 1
            e = self.or_()
            assert self.next() == ("p", ")"), "missing )"
            return e
        return self.clause()

    def clause(self):
        field = self.next()[1]
        k, v = self.peek()
        if k == "w" and v.upper() == "NOT":
            self.i += 1
            assert self.next()[1].upper() == "IN"
            op = "not in"
        elif k == "w" and v.upper() == "IN":
            self.i += 1
            op = "in"
        elif k == "w" and v.upper() == "IS":
            self.i += 1
            op = "is"
            if self.at_word("NOT"):
                self.i += 1
                op = "is not"
        else:
            op = self.next()[1]
        return ("cmp", field.lower(), op, self.value())

    def value(self):
        k, v = self.next()
        if (k, v) == ("p", "("):
            vals = []
            while self.peek() != ("p", ")"):
                vals.append(self.value())
                if self.peek() == ("p", ","):
                    self.i += 1
            self.i += 1
            return ("list", vals)
        if k == "s":
            return ("str", v)
        if self.peek() == ("p", "(") and self.i < len(self.t) - 1 and self.t[self.i + 1] == ("p", ")"):
            self.i += 2
            return ("fn", v)
        if v.upper() == "EMPTY" or v.upper() == "NULL":
            return ("empty", None)
        return ("str", v)


# ---------------------------------------------------------------- the fake

class FakeJira:
    def __init__(self, ap, clock, kind="sepm"):
        self.ap, self.clock, self.kind = ap, clock, kind
        self.p = PROJECTS[kind]
        self.dry = False
        self.actor = "bot"
        self.issues = {}
        self.n = 0
        self.props = {}
        self.sprints = {}
        self.writes = []      # (time, actor, key, what)
        self.closed = []      # (sprint name, done, total) at the moment each sprint closed
        self.fail_myself = None
        self.calls = 0
        self.lag = False      # tests can freeze search results to mimic index lag
        self.lagged = {}
        for name in self.p["epics"]:
            self._new("Epic", name, components=[self.p["components"][0]] if self.p["components"] else [])

    # ------------------------------------------------ helpers
    def now(self):
        return self.clock()

    def err(self, code, method, path, msg):
        raise self.ap.JiraError(code, f"{method} {path} -> {code} {msg}")

    def _new(self, itype, summary, **kw):
        self.n += 1
        key = f"PER-{self.n}"
        t = self.now()
        self.issues[key] = {
            "key": key, "type": itype, "summary": summary, "status": self.p["start"],
            "priority": self.p["default_priority"], "duedate": None, "created": t, "updated": t,
            "parent": None, "components": kw.get("components", []), "assignee": None, "cf": {},
            "sprints": [], "comments": [], "worklogs": [], "history": [], "rank": float(self.n),
        }
        return key

    def _change(self, issue, field, old, new):
        if old == new:
            return
        issue["history"].append((self.now(), self.actor, field, old, new))
        issue["updated"] = self.now()
        self.writes.append((self.now(), self.actor, issue["key"], field))

    def epics(self):
        return {k for k, i in self.issues.items() if i["type"] == "Epic"}

    def cat(self, status):
        return "Done" if status in self.p["done"] else "In Progress" if status in self.p["doing"] else "To Do"

    def open_sprints(self):
        return {s["id"] for s in self.sprints.values() if s["state"] in ("active", "future")}

    def view(self, i, fields):
        out = {}
        for f in fields:
            if f == "summary":
                out[f] = i["summary"]
            elif f == "status":
                out[f] = {"name": i["status"], "statusCategory": {"name": self.cat(i["status"])}}
            elif f == "duedate":
                out[f] = i["duedate"]
            elif f == "priority":
                out[f] = {"name": i["priority"], "id": str(self.p["priorities"].index(i["priority"]) + 1)}
            elif f in ("created", "updated"):
                out[f] = jira_time(i[f])
            elif f == "parent":
                out[f] = {"key": i["parent"], "fields": {"summary": self.issues[i["parent"]]["summary"]}} if i["parent"] else None
            elif f == "components":
                out[f] = [{"name": c} for c in i["components"]]
            elif f == "customfield_10020":
                out[f] = [{"id": s, "name": self.sprints[s]["name"], "state": self.sprints[s]["state"]} for s in i["sprints"]] or None
            elif f.startswith("customfield_"):
                v = i["cf"].get(f)
                out[f] = {"value": v, "id": v} if f in self.p["options"] and v else v
        return {"key": i["key"], "fields": out}

    # ------------------------------------------------ JQL evaluation
    def field_value(self, i, f):
        if f == "project":
            return "PER"
        if f == "issuetype":
            return i["type"]
        if f == "status":
            return i["status"]
        if f == "statuscategory":
            return self.cat(i["status"])
        if f in ("created", "updated"):
            return i[f]
        if f == "duedate":
            return datetime.strptime(i["duedate"], "%Y-%m-%d").date() if i["duedate"] else None
        if f == "sprint":
            return i["sprints"]
        if f == "key":
            return i["key"]
        if f == "priority":
            return i["priority"]
        if f == "parent":
            return i["parent"]
        if f == "summary":
            return i["summary"]
        m = re.fullmatch(r"cf\[(\d+)\]", f)
        if m:
            v = i["cf"].get(f"customfield_{m.group(1)}")
            if f"customfield_{m.group(1)}" in DATE_FIELDS and v:
                return datetime.strptime(v, "%Y-%m-%d").date()
            return v
        raise ValueError(f"fake JQL does not know field {f}")

    def resolve(self, val, f):
        kind, v = val
        local = self.now().astimezone(TZ)
        if kind == "list":
            out = set()
            for x in v:
                r = self.resolve(x, f)
                out |= r if isinstance(r, set) else {r}
            return out
        if kind == "fn":
            if v == "openSprints":
                return self.open_sprints()
            if v == "startOfDay":
                return local.replace(hour=0, minute=0, second=0, microsecond=0)
            if v == "endOfDay":
                return local.replace(hour=23, minute=59, second=59, microsecond=0)
            if v == "currentUser":
                return "me"
            raise ValueError(f"fake JQL does not know function {v}()")
        if kind == "empty":
            return None
        m = re.fullmatch(r"(-?\d+)d", v)
        if m:
            return self.now() + timedelta(days=int(m.group(1)))
        if f == "sprint" and re.fullmatch(r"\d+", v):
            return int(v)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return datetime.strptime(v, "%Y-%m-%d").date()
        return v

    def ev(self, e, i):
        if e is None:
            return True
        op = e[0]
        if op == "and":
            return self.ev(e[1], i) and self.ev(e[2], i)
        if op == "or":
            return self.ev(e[1], i) or self.ev(e[2], i)
        if op == "not":
            return not self.ev(e[1], i)
        _, f, cmp, val = e
        x = self.field_value(i, f)
        if cmp in ("is", "is not"):
            empty = x is None or x == []
            return empty if cmp == "is" else not empty
        y = self.resolve(val, f)
        if isinstance(x, datetime) and not isinstance(y, datetime) and hasattr(y, "year"):
            x = x.astimezone(TZ).date()
        if not isinstance(x, datetime) and hasattr(x, "year") and isinstance(y, datetime):
            y = y.astimezone(TZ).date()
        low = lambda v: v.lower() if isinstance(v, str) else v
        if isinstance(x, list):  # sprint
            ys = y if isinstance(y, set) else {y}
            if cmp in ("=", "in"):
                return any(s in ys for s in x)
            if cmp in ("!=", "not in"):
                return bool(x) and not any(s in ys for s in x)
            raise ValueError(cmp)
        if x is None:
            return False
        if cmp == "=":
            return low(x) == low(y)
        if cmp == "!=":
            return low(x) != low(y)
        if cmp == "in":
            return low(x) in {low(v) for v in y}
        if cmp == "not in":
            return low(x) not in {low(v) for v in y}
        if cmp == "~":
            return low(str(y)) in low(str(x))
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[cmp]

    def search_all(self, jql):
        q = JQL(jql)
        hits = [i for i in self.issues.values() if self.ev(q.expr, i)]
        for f, desc in reversed(q.order):
            f = f.lower()
            if f == "duedate":
                hits.sort(key=lambda i: (i["duedate"] is None, i["duedate"] or ""), reverse=False)
                if desc:
                    hits.reverse()
            else:
                hits.sort(key=lambda i: i[f] if f in ("created", "updated", "rank") else str(i.get(f)), reverse=desc)
        return hits

    # ------------------------------------------------ REST surface
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

    def req(self, method, path, body=None, params=None, write=False):
        self.clock.advance(0.4)  # a real call takes a moment, so two calls never share a timestamp
        self.calls += 1
        if self.dry:
            if self.calls > 3000:
                raise AssertionError("a dry run is looping")
            if write:  # like the real client: a dry run changes nothing
                return {}
        p = path
        params = params or {}
        if method == "GET":
            if p == "/rest/api/3/myself":
                if self.fail_myself:
                    self.err(self.fail_myself, method, p, '{"message":"Client must be authenticated"}')
                return {"accountId": "acc-1", "displayName": "Test Student"}
            if p == "/rest/api/3/project/PER":
                return {"name": "Personal"}
            if p == "/rest/api/3/project/PER/statuses":
                return [{"name": "Task", "subtask": False, "statuses": [{"name": s} for s in self.p["statuses"]]},
                        {"name": "Epic", "subtask": False, "statuses": [{"name": s} for s in self.p["statuses"]]}]
            if p == "/rest/api/3/field":
                return [{"id": i, "name": n} for i, n in self.p["fields"]] + [{"id": "summary", "name": "Summary"}]
            if p == "/rest/api/3/project/PER/components":
                return [{"name": c} for c in self.p["components"]]
            if p.startswith("/rest/api/3/user/properties/"):
                if p not in self.props:
                    self.err(404, method, p, "{}")
                return {"value": json.loads(self.props[p])}
            m = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)(/transitions|/editmeta)?", p)
            if m:
                i = self.issues.get(m.group(1))
                if not i:
                    self.err(404, method, p, "{}")
                if m.group(2) == "/transitions":
                    return {"transitions": [{"id": str(self.p["statuses"].index(s)), "to": {"name": s}}
                                            for s in self.p["flows"][i["status"]]]}
                if m.group(2) == "/editmeta":
                    fields = {"priority": {"allowedValues": [{"id": str(n + 1), "name": v}
                                                             for n, v in enumerate(self.p["priorities"])]}}
                    for f, vals in self.p["options"].items():
                        fields[f] = {"allowedValues": [{"id": v, "value": v} for v in vals]}
                    return {"fields": fields}
                fields = params.get("fields", "summary,status").split(",")
                return self.view(i, fields)
            if p == "/rest/agile/1.0/board":
                return {"values": [{"id": 6, "name": "Main Board"}]}
            m = re.fullmatch(r"/rest/agile/1.0/board/6/sprint", p)
            if m:
                states = set(params.get("state", "active,future,closed").split(","))
                vals = [dict(s) for s in sorted(self.sprints.values(), key=lambda s: s["id"]) if s["state"] in states]
                total = len(vals)
                if params.get("maxResults"):
                    vals = vals[:int(params["maxResults"])]
                return {"values": vals, "total": total}
            raise AssertionError(f"fake Jira: unhandled GET {p}")

        if method == "POST" and p == "/rest/api/3/search/jql":
            hits = self.search_all(body["jql"])
            if self.lag:  # real Jira's search index trails changes by a few seconds
                hits = [self.lagged.get(i["key"], i) for i in hits]
            start = int(body.get("nextPageToken") or 0)
            page = hits[start:start + body.get("maxResults", 50)]
            more = start + len(page) < len(hits)
            out = {"issues": [self.view(i, body["fields"]) for i in page], "isLast": not more}
            if more:
                out["nextPageToken"] = str(start + len(page))
            return out

        if method == "PUT" and p.startswith("/rest/api/3/user/properties/"):
            raw = json.dumps(body)
            if len(raw) > 32768:
                self.err(400, method, p, '{"errorMessages":["The property value is too large"]}')
            self.props[p] = raw
            return {}

        if method == "POST" and p == "/rest/api/3/issue":
            f = body["fields"]
            itype = f["issuetype"]["name"]
            comps = [c["name"] for c in f.get("components") or []]
            if itype == "Epic" and self.p["epic_needs_component"] and not comps:
                self.n += 1  # Jira burns the number even when it refuses
                self.err(400, method, p, '{"errorMessages":["Field Components is required."]}')
            key = self._new(itype, f["summary"], components=comps)
            self.writes.append((self.now(), self.actor, key, "create"))
            return {"key": key}

        m = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)(/transitions|/comment|/worklog)?", p)
        if m:
            i = self.issues.get(m.group(1))
            if not i:
                self.err(404, method, p, "{}")
            what = m.group(2)
            if method == "POST" and what == "/transitions":
                target = self.p["statuses"][int(body["transition"]["id"])]
                if target not in self.p["flows"][i["status"]]:
                    self.err(400, method, p, '{"errorMessages":["Transition not allowed"]}')
                if i["status"] in self.p["needs_parent_to_leave"] and not i["parent"]:
                    self.err(400, method, p, '{"errorMessages":["Field Parent is required."]}')
                if target in self.p["needs_component_to_enter"] and not i["components"]:
                    self.err(400, method, p, '{"errorMessages":["Field Components is required."]}')
                self._change(i, "status", i["status"], target)
                i["status"] = target
                return {}
            if method == "POST" and what == "/comment":
                text = body["body"]["content"][0]["content"][0]["text"]
                i["comments"].append((self.now(), self.actor, text))
                i["updated"] = self.now()
                self.writes.append((self.now(), self.actor, i["key"], "comment"))
                return {}
            if method == "POST" and what == "/worklog":
                i["worklogs"].append((self.now(), self.actor, body["timeSpentSeconds"], body["started"]))
                i["updated"] = self.now()
                self.writes.append((self.now(), self.actor, i["key"], "worklog"))
                return {}
            if method == "PUT" and not what:
                return self.edit(i, body["fields"], method, p)

        if method == "PUT" and p == "/rest/agile/1.0/issue/rank":
            i, before = self.issues[body["issues"][0]], self.issues[body["rankBeforeIssue"]]
            self._change(i, "rank", i["rank"], before["rank"] - 0.5)
            i["rank"] = before["rank"] - 0.5
            return {}

        if method == "POST" and p == "/rest/agile/1.0/sprint":
            sid = len(self.sprints) + 1
            self.sprints[sid] = {"id": sid, "name": body["name"], "state": "future", "originBoardId": 6}
            return dict(self.sprints[sid])
        m = re.fullmatch(r"/rest/agile/1.0/sprint/(\d+)(/issue)?", p)
        if m and method == "POST":
            s = self.sprints[int(m.group(1))]
            if m.group(2):
                for k in body["issues"]:
                    i = self.issues[k]
                    old = list(i["sprints"])
                    i["sprints"] = [x for x in i["sprints"] if x not in self.open_sprints()] + [s["id"]]
                    self._change(i, "sprint", old, list(i["sprints"]))
                return {}
            if body.get("state") == "active":
                if s["state"] != "future" or not body.get("startDate") or not body.get("endDate"):
                    self.err(400, method, p, '{"errorMessages":["Sprint cannot be started"]}')
                if any(x["state"] == "active" for x in self.sprints.values()):
                    self.err(400, method, p, '{"errorMessages":["Another sprint is already active"]}')
            if body.get("state") == "closed":
                if s["state"] != "active":
                    self.err(400, method, p, '{"errorMessages":["Only active sprints can be closed"]}')
                inside = [i for i in self.issues.values() if s["id"] in i["sprints"]]
                done = sum(1 for i in inside if i["status"] in self.p["done"])
                self.closed.append((s["name"], done, len(inside), self.now()))
            s.update({k: v for k, v in body.items() if k in ("name", "state", "goal", "startDate", "endDate")})
            return dict(s)
        raise AssertionError(f"fake Jira: unhandled {method} {p}")

    def edit(self, i, fields, method, p):
        known = {"summary", "description", "duedate", "assignee", "components", "priority", "parent", "labels"}
        for f in fields:
            if f in self.p["unsettable"]:
                self.err(400, method, p, json.dumps({"errors": {f: f"Field '{f}' cannot be set. It is not on the "
                                                                   "appropriate screen, or unknown."}}))
            if f not in known and f not in {x for x, _ in self.p["fields"]}:
                self.err(400, method, p, json.dumps({"errors": {f: "unknown field"}}))
        if "components" in fields:
            for c in fields["components"] or []:
                if c["name"] not in self.p["components"]:
                    self.err(400, method, p, '{"errors":{"components":"Component does not exist"}}')
        if fields.get("parent") and fields["parent"]["key"] not in self.epics():
            self.err(400, method, p, '{"errors":{"parent":"Epic does not exist"}}')
        for f, v in fields.items():
            if f == "summary":
                self._change(i, f, i["summary"], v)
                i["summary"] = v
            elif f == "duedate":
                self._change(i, f, i["duedate"], v)
                i["duedate"] = v
            elif f == "components":
                new = [c["name"] for c in v or []]
                self._change(i, f, i["components"], new)
                i["components"] = new
            elif f == "priority":
                new = self.p["priorities"][int(v["id"]) - 1]
                self._change(i, f, i["priority"], new)
                i["priority"] = new
            elif f == "parent":
                self._change(i, f, i["parent"], v["key"] if v else None)
                i["parent"] = v["key"] if v else None
            elif f == "assignee":
                self._change(i, f, i["assignee"], v)
                i["assignee"] = v
            elif f.startswith("customfield_"):
                new = v["id"] if isinstance(v, dict) else v
                self._change(i, f, i["cf"].get(f), new)
                i["cf"][f] = new
        return {}

    # ------------------------------------------------ a person using the same board
    def human(self, fn):
        self.actor = "human"
        try:
            return fn()
        finally:
            self.actor = "bot"
