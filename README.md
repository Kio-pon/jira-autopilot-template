# Jira Autopilot

Keeps your Jira project active without you opening Jira. It follows a normal student rhythm:

| When | What happens |
|---|---|
| Every 2 to 3 hours, random gaps, 9am to 10pm | 1 to 3 new tasks land in your inbox. Mix of quick and long, everyday life and your courses. |
| Morning, random time 8:00 to 9:30 | Reviews deferred tasks, re-defers some, starts 1 to 3 tasks. |
| Night, random time 10:00 to 11:30 | Triages the inbox, sets epic, component, due date, story points, assignee, priority. Moves work along and finishes some. |
| Every 2 days, before the night run | Plans, starts and closes 1-week sprints. |
| Whenever you open an issue | Your real tasks land in the inbox with your times and due dates. Mixed in with the rest, never closed for you. |
| About 1 day in 4, at a random time | Something urgent comes up: one task due today, top priority, pulled into the sprint and started. A few hours later it goes to review and gets closed. Now and then it slips a day. |

It runs free on GitHub Actions, so your laptop can stay off. It uses **zero AI tokens** day to day. You use Gemini once, at setup, if you want personal tasks.

It works on any Jira Cloud project. It auto-detects your statuses, fields and boards. It fits the SEPM course template (07 Inbox, 08 Defer, 06 Process Today and so on) out of the box and adapts to a plain To Do / In Progress / Done project.

---

## Setup (about 10 minutes)

### 1. Get a Jira API token
1. Open https://id.atlassian.com/manage-profile/security/api-tokens
2. **Create API token**, name it `autopilot`, copy it somewhere safe.
3. Note your site (like `yourname.atlassian.net`) and your project key (the letters before issue numbers, like `PER` in `PER-12`).

### 2. Put this folder on GitHub
1. Make a free GitHub account if you don't have one.
2. If you got a GitHub link: click **Use this template → Create a new repository**, name it `jira-autopilot`, set it to **Private**. Skip to step 3.
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

That's it. From now on it runs by itself every 30 minutes and does whatever is due.

**First run.** The first scheduled run between 9am and 10pm sets up your board: it adds 4 to 6 tasks, sorts them, starts a 1-week sprint and starts one task, spread over about 10 minutes. It happens once. Don't want to wait for the schedule? **Run workflow → kickoff** does it now.

### 5. Optional: personal tasks (one Gemini message)
Out of the box it uses a built-in list of about 140 everyday tasks. To add tasks about your own courses and projects:
1. **Run workflow → audit**. Copy the output.
2. Open `SETUP_PROMPT.md`, fill in "About me", paste the audit output, send it to Gemini at https://gemini.google.com.
3. Save its reply as `tasks.json` in the repo (**Add file → Create new file**, name it `tasks.json`, paste, commit).

`tasks.example.json` shows the format. The reply also has a short `urgent` list, used on urgent days. Without one, it uses built-in urgent tasks.

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

Within a minute it adds them to your inbox, replies with the new keys, and closes the issue. The night run sorts them with everything else.
**Your own tasks are never closed, deferred or re-dated for you.** Only a `done` line or you in Jira closes them.
If you added a Gemini key (see Optional: Gemini), you can also write free text, like "finish the DB lab by friday, about 2 hours". Without a key, plain text still works for simple lines like `Finish DB lab by fri, 2h`.
Only issues you open count. Anyone else's are ignored.

---

## Settings (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `utc_offset_hours` | `5` | Your time zone. Pakistan is 5. |
| `active_hours` | `[9, 22]` | Hours when new tasks may appear. |
| `morning_window`, `night_window` | `08:00-09:30`, `22:00-23:30` | Random time picked inside each window, daily. Keep both before midnight. |
| `drop_gap_hours` | `[2, 3]` | Random gap between inbox drops. |
| `everyday_ratio` | `0.55` | Share of everyday life tasks vs course and project tasks. |
| `quick_ratio` | `0.4` | Share of quick (under 10 min) tasks. |
| `sprint_every_days` | `2` | How often sprints get checked. |
| `sprint_length_days` | `7` | Sprint length. |
| `kickoff_tasks` | `[4, 6]` | How many tasks the first run adds before starting the first sprint. |
| `urgent_day_chance` | `0.25` | Share of days that get one urgent task. `0` turns it off. |
| `urgent_work_hours` | `[1, 4]` | Hours an urgent task stays In Progress before review. |
| `urgent_slip_chance` | `0.15` | Share of urgent tasks that slip to the next day instead of closing. |
| `jitter_minutes` | `0` | Extra random wait (minutes) before acting, so actions don't land on :07 and :37. Each wait costs Actions minutes, so keep it 0 on a private repo. On a public repo or your own computer, 20 is a good value. |
| `touch_existing_tasks` | `false` | `false`: only touches tasks it created. `true`: also triages, moves and closes your existing tasks. |
| `label` | `""` | Put a label like `"auto"` here to tag every task it creates. |
| `allow_create_epics` | `true` | May create one new epic per night when nothing fits. |
| `gemini_model` | `gemini-3.8-flash` | Only change this if Google retires the model. |

If your statuses have unusual names and `check` shows "-" for one you have, add your name to the matching list under `statuses` in `config.json`, for example `"statuses": {"review": ["QA"]}`.

If you're not in Pakistan, also shift the hours in `.github/workflows/autopilot.yml`. The comment there explains how.

---

## Optional: Gemini (free)
Add one more secret, `GEMINI_API_KEY`, with a free key from https://aistudio.google.com/apikey (sign in with Google, **Create API key**). Nothing else to set.

It turns on two things:
- **Fresh ideas.** When your personal task list runs low, it asks Gemini for 25 new tasks. About one request every week or two.
- **Free-text issues.** Write tasks however you like, for example "finish the DB lab by friday, about 2 hours". Gemini turns them into proper tasks with times and dates.

It uses `gemini-3.8-flash`, Google's default free model. That's a handful of requests a month, far under the free limits. Without a key, everything else works the same: it reuses the built-in and personal lists, never repeating a title within 3 weeks, and issues use the `title | time | due` format.
Note: on Google's free tier, what you send may be used to improve Google's products. Don't put anything private in your tasks.

---

## Run it on your own computer instead
1. Install Python 3.9 or newer.
2. Copy `.env.example` to `.env` and fill it in.
3. `python autopilot.py check`
4. Schedule `python autopilot.py tick` every 30 minutes: Task Scheduler on Windows, `crontab -e` on Mac or Linux (`*/30 * * * * cd /path/to/jira-autopilot && python3 autopilot.py tick`).

The computer has to be on for it to run. GitHub Actions avoids that.

## Commands
```
python autopilot.py check      verify login and show the mapping
python autopilot.py audit      short summary for the setup prompt
python autopilot.py tick       run whatever is due (what the schedule calls)
python autopilot.py kickoff    first-run setup now: a few tasks and a starter sprint
python autopilot.py urgent     an urgent task comes up now (the schedule closes it hours later)
python autopilot.py inbox      add your own tasks from ISSUE_TITLE / ISSUE_BODY (what the issue workflow runs)
python autopilot.py drop       add inbox items now  (--count 2 for a fixed number)
python autopilot.py morning    run the morning routine now
python autopilot.py night      run the night routine now
python autopilot.py sprint     run sprint planning now
python autopilot.py refill     fetch fresh task ideas from Gemini now (needs GEMINI_API_KEY)
```
Add `--dry-run` to any of them to preview.

## Pause or stop
**Actions → jira-autopilot → ⋯ → Disable workflow.** Enable it again the same way.

## FAQ
**Where does it remember things?** In a small private property on your own Jira user account, so it works from anywhere. It falls back to a local `state.json` if that's blocked.

**GitHub Actions minutes?** About 34 short runs a day, roughly 1,000 minutes a month. Private repos get 2,000 free minutes.

**It says "no direct transition".** Your workflow doesn't allow that jump. It skips and carries on. Allow the transition in Jira, or ignore it.

**Something went wrong.** Open the latest run in the Actions tab. The log says exactly what it tried.
