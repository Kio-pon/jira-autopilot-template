# Jira Autopilot

Keeps your Jira project active without you opening Jira, on the rhythm of a real student's week:

| When | What happens |
|---|---|
| Every 3 to 4.5 hours, 9am to 10pm | 1 or 2 new tasks land in your inbox. A mix of quick and longer ones, everyday life and your courses. About 3 in 10 have no due date, like real to-dos. |
| Morning, 8:00 to 9:30 (later at weekends) | Reviews deferred tasks and starts the next thing, never more than 2 tasks in progress at once. |
| Night, starting 21:45 to 22:50, over three runs about half an hour apart | Sorts the inbox (epic, component, due date, points, priority), works through what's in progress, then sprint upkeep. Bigger tasks take several evenings and log their time. Small chores just get done. |
| Every week, on the sprint's end date | Closes the sprint and plans the next one from how much actually got done last time, so sprints end mostly done, not perfectly. |
| About 1 day in 4, at a random time | Something urgent comes up: a task due today goes into the sprint and gets started, then reviewed and closed a few hours later. Now and then it slips a day. |
| Weekends | Fewer and lighter tasks, a later start, fewer hours of work. |
| About 1 day in 12 | Nothing happens at all. People have those days. |
| Days you use Jira yourself | It notices your own changes and keeps out of your way. |
| Whenever you open an issue | Your real tasks land in the inbox with your times and due dates. They get planned, never closed for you. |

Details that keep it believable: a few seconds between clicks and half a minute between tasks, comment phrases that don't repeat within two weeks, an occasional typo in a title that gets fixed later, small edits during the day (re-ranking, nudging a date), and a night that sometimes only gets halfway through the inbox.

It runs free on GitHub Actions, so your laptop can stay off. It uses **zero AI tokens** day to day.

It works on any Jira Cloud project. It detects your statuses, fields and boards by itself. It fits the SEPM course template (07 Inbox, 08 Defer, 06 Process Today and so on) out of the box, including its rules (an epic before a task leaves the inbox, a component before it enters the semester backlog). It also adapts to a plain To Do / In Progress / Done project.

---

## Setup (about 10 minutes)

> **Keep your copy private. Don't share this repo or its code.** See `LICENSE`: personal use only.

### 1. Get a Jira API token
1. Open https://id.atlassian.com/manage-profile/security/api-tokens
2. **Create API token**, name it `autopilot`, copy it somewhere safe.
3. Note your site (like `yourname.atlassian.net`) and your project key (the letters before issue numbers, like `PER` in `PER-12`).

### 2. Put this folder on GitHub
1. Make a free GitHub account if you don't have one.
2. If you got a GitHub link: click **Use this template → Create a new repository**, pick **your own account** as the owner, name it `jira-autopilot`, set it to **Private**. Skip to step 3.
3. If you got a zip: New repository, name it `jira-autopilot`, set it to **Private**. Upload every file in the folder, including the hidden `.github` folder. Easiest: on the repo page click **uploading an existing file**, then drag the whole folder in.

### 3. Add your secrets and one variable
Repo **Settings → Secrets and variables → Actions → New repository secret**. Add these three:

| Name | Value |
|---|---|
| `JIRA_SITE` | `yourname.atlassian.net` |
| `JIRA_EMAIL` | the email you log in to Jira with |
| `JIRA_TOKEN` | the token from step 1 |

Then switch to the **Variables** tab on the same page and click **New repository variable**:

| Name | Value |
|---|---|
| `JIRA_PROJECT` | your project key, like `PER` |

It's a variable, not a secret, because GitHub hides secret values in the run logs. As a secret, every task key would show as `***-98` instead of `PER-98`.

### 4. Test it
1. Repo **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **jira-autopilot → Run workflow**, choose `check`, run it.
3. Open the run. You should see your name, project, and how each status mapped. "Ready." means you're set.
4. Optional: run `drop` with **Dry run** ticked to preview tasks without changing anything.

That's it. From now on it runs by itself and does whatever is due.

**First run.** The first scheduled run between 9am and 10pm sets up your board: 4 to 6 tasks, sorted, a 1-week sprint started and one task in progress, spread over about 10 minutes. It happens once. To skip the wait, **Run workflow → kickoff**.

### 5. Optional: personal tasks
Out of the box it uses a built-in list of about 140 everyday tasks. To add tasks about your own courses and projects, pick one:

- **With a Gemini key** (see Optional: Gemini below): open a new issue titled `setup` and describe yourself in the body: uni and year, your courses with a short name each (`Database Systems (DB)`), your projects, and what a normal week looks like. Within a minute it writes `tasks.json` for you, commits it, and replies with what it saved. Open another `setup` issue any time to redo it.
- **Without a key:** run **Run workflow → audit** and copy the output. Open `SETUP_PROMPT.md`, fill in "About me", paste the audit output, and send it all to Gemini at https://gemini.google.com. Save the reply as `tasks.json` in the repo (**Add file → Create new file**, name it `tasks.json`, paste, commit).

`tasks.example.json` shows the format. It includes a short `urgent` list for urgent days. Without one, it uses built-in urgent tasks.

### 6. Optional: add your real tasks from your phone
Open a new **issue** in your repo (the GitHub app works) and write one task per line:

```
Finish DB lab report | 2h | fri
Call the bank about the card | 10m | today
Prepare SEPM presentation | 4h | 24 oct | SEPM
done PER-42
```

- **Format:** `title | time | due | epic`. Everything after the title is optional, in any order. A bare title works too.
- **Time:** `10m`, `45 min`, `2h`, `1h30m`. It sets the size and story points: up to 15 min is quick (1 point), about 1h is 2, 2h is 3, 4h is 5, longer is 8.
- **Due:** `today`, `tmrw`, `fri`, `in 3 days`, `24 oct`, `2026-11-02`.
- **Epic:** optional. Without one, it picks the epic whose name matches a word in the title.
- **`done ...`:** a line starting with `done` closes one of your own tasks, by key or by title.

Within a minute it adds them to your inbox, replies with the new keys, and closes the issue. The night run sorts them with everything else, and sprint planning includes them.
**Your own tasks are never worked on, closed, deferred or re-dated for you.** Only a `done` line or you in Jira closes them.
With a Gemini key you can also write free text, like "finish the DB lab by friday, about 2 hours". Without one, simple lines like `Finish DB lab by fri, 2h` still work.
Only issues you open count. Anyone else's are ignored.

---

## If something breaks
It opens one issue in your repo titled **Autopilot needs attention**, and GitHub emails you about it. It happens when your Jira token expires (Atlassian tokens last at most a year) or when three runs in a row fail. The issue says what to do, and it closes itself on the next run that works. You won't get a failed-run email every half hour.

## Getting updates
Copies made from a template don't update by themselves. If the person who shared this gave you an **update key**, add it as a secret named `TEMPLATE_KEY`. Every Monday the `jira-autopilot-update` workflow then fetches the latest version, runs its tests, and only applies it if they pass. **Actions → jira-autopilot-update → Run workflow** updates right away.

It never touches your `config.json`, `tasks.json` or secrets. GitHub doesn't let a workflow change workflow files, so if those changed, the run says so and you copy them from the template by hand.

---

## Settings (`config.json`)

Anything not in your `config.json` uses the default below.

| Setting | Default | Meaning |
|---|---|---|
| `utc_offset_hours` | `5` | Your time zone. Pakistan is 5. |
| `active_hours` | `[9, 22]` | Hours when new tasks may appear. |
| `morning_window` | `["08:00", "09:30"]` | Random morning time picked inside this, daily. |
| `weekend_morning_window` | `["09:30", "11:30"]` | The same at weekends. |
| `night_window` | `["21:45", "22:50"]` | When the night routine starts. It takes about an hour over three runs. |
| `drop_gap_hours` | `[3.0, 4.5]` | Gap between new tasks on weekdays. |
| `weekend_drop_gap_hours` | `[4.0, 6.0]` | The same at weekends. |
| `weekend_days` | `[5, 6]` | Saturday and Sunday. Use `[4, 5]` for a Friday-Saturday weekend. |
| `everyday_ratio` | `0.55` | Share of everyday life tasks vs course and project tasks. |
| `weekend_everyday_ratio` | `0.8` | The same at weekends. |
| `quick_ratio` | `0.4` | Share of quick (under 10 min) tasks. |
| `no_due_ratio` | `0.3` | Share of new tasks without a due date. |
| `daily_hours`, `weekend_hours` | `[3, 5]`, `[1, 3.5]` | Hours of work done per evening. |
| `wip_limit` | `2` | Most tasks in progress at once. |
| `quiet_day_chance` | `0.08` | Share of days with nothing at all. Never two in a row. |
| `days_off` | `[]` | Dates with nothing at all, like `["2026-12-25", "2026-12-28..2027-01-03"]`. |
| `lazy_night_chance` | `0.2` | Share of nights that only sort part of the inbox. |
| `log_work` | `true` | Log time on tasks as they get worked on. |
| `edit_chance` | `0.45` | Share of days with one small edit (re-rank, date nudge, priority bump). |
| `typo_chance` | `0.06` | Share of new tasks with a small typo that gets fixed later. |
| `back_off` | `true` | Keep out of the way on days you change tasks in Jira yourself. |
| `sprint_length_days` | `7` | Sprint length. |
| `sprint_size` | `[6, 12]` | Rough number of tasks per sprint. How many points actually fit comes from what got done in past sprints. |
| `sprint_every_days` | `2` | How often mid-sprint upkeep runs (pulling in one urgent task if there's room). |
| `kickoff_tasks` | `[4, 6]` | How many tasks the first run adds before starting the first sprint. |
| `urgent_day_chance` | `0.25` | Share of days that get one urgent task. `0` turns it off. |
| `urgent_work_hours` | `[1, 4]` | Hours an urgent task stays In Progress before review. |
| `urgent_slip_chance` | `0.15` | Share of urgent tasks that slip to the next day. |
| `touch_existing_tasks` | `false` | `false`: only touches tasks it created. `true`: also sorts, moves and closes your existing tasks. |
| `label` | `""` | Put a label like `"auto"` here to tag every task it creates. |
| `allow_create_epics` | `true` | May create a new epic when nothing fits. |
| `gemini_model` | `gemini-3.8-flash` | Only change this if Google retires the model. |

If your statuses have unusual names and `check` shows "-" for one you have, add your name to the matching list under `statuses` in `config.json`, for example `"statuses": {"review": ["QA"]}`.

If you're not in Pakistan, also shift the hours in `.github/workflows/autopilot.yml`. The comment there explains how.

---

## Optional: Gemini (free)
Add one more secret, `GEMINI_API_KEY`, with a free key from https://aistudio.google.com/apikey (sign in with Google, **Create API key**). Nothing else to set.

It turns on:
- **Setup from an issue.** Step 5 above, one request.
- **Fresh ideas.** When your personal task list runs low, it asks for 25 new tasks, at most once every three days.
- **Fresh comment phrases.** Once a week, 40 new ways to say "started this" or "done", so comments never sound canned.
- **Free-text issues.** Write tasks however you like and Gemini turns them into proper tasks with times and dates.

It uses `gemini-3.8-flash`, Google's default free model. That's a few requests a week, far under the free limits. Without a key, everything else works the same.
Note: on Google's free tier, what you send may be used to improve Google's products. Don't put anything private in your tasks.

---

## Tests
`python3 tests/run_tests.py` runs everything without a Jira account: the issue parser, the alert and setup logic, then four 30-day simulations on a fake Jira that follows the SEPM template's rules. It checks that your own tasks are never touched, sprints end mostly but not perfectly done, big tasks take days, nothing happens in bursts, comments don't repeat, weekends are lighter, it backs off when you use Jira, and the backlog stays manageable. It takes about 10 seconds. `--long` runs eight 90-day semesters. The `tests` workflow runs it on every push.

## Run it on your own computer instead
1. Install Python 3.9 or newer.
2. Copy `.env.example` to `.env` and fill it in.
3. `python autopilot.py check`
4. Schedule `python autopilot.py tick` every 30 minutes: Task Scheduler on Windows, `crontab -e` on Mac or Linux (`*/30 * * * * cd /path/to/jira-autopilot && python3 autopilot.py tick`).

The computer has to be on for it to run. GitHub Actions avoids that.

## Commands
```
python autopilot.py check      verify login and show the mapping
python autopilot.py audit      short summary of your project
python autopilot.py tick       run whatever is due (what the schedule calls)
python autopilot.py kickoff    first-run setup now: a few tasks and a starter sprint
python autopilot.py urgent     an urgent task comes up now (the schedule closes it hours later)
python autopilot.py inbox      add your own tasks from ISSUE_TITLE / ISSUE_BODY (what the issue workflow runs)
python autopilot.py setup      write tasks.json from ISSUE_BODY with Gemini (what a "setup" issue runs)
python autopilot.py drop       add inbox items now  (--count 2 for a fixed number)
python autopilot.py morning    run the morning routine now
python autopilot.py night      run all three night steps now
python autopilot.py sprint     run sprint planning now
python autopilot.py edit       make one small edit now
python autopilot.py refill     fetch fresh task ideas from Gemini now (needs GEMINI_API_KEY)
```
Add `--dry-run` to any of them to preview.

## Pause or stop
**Actions → jira-autopilot → ⋯ → Disable workflow.** Enable it again the same way. For a holiday, list the dates in `days_off` instead.

## FAQ
**Where does it remember things?** In a small private property on your own Jira user account, so it works from anywhere. It falls back to a local `state.json` if that's blocked.

**GitHub Actions minutes?** 21 short runs a day (hourly, every 30 minutes at night). About 750 to 850 minutes a month. Private repos get 2,000 free minutes.

**It says "no direct transition".** Your workflow doesn't allow that jump. It skips and carries on. Allow the transition in Jira, or ignore it.

**It says a field "is not on your issue screen".** Jira won't let anyone set that field, so it stops trying. Story points still count for sprint planning.

**Something went wrong.** Open the latest run in the Actions tab. The log says exactly what it tried.
