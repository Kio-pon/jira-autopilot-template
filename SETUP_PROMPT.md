# One-time personalisation prompt

Paste it into Gemini at https://gemini.google.com (free, same Google account as your API key). It takes one message.

1. Run `python autopilot.py audit` (or the **audit** option in the GitHub Actions "Run workflow" menu) and copy its output.
2. Fill in the "About me" part below, paste the audit output where shown, and send the whole thing to Gemini.
3. Save the JSON it returns as `tasks.json` next to `autopilot.py`.

Skip this step entirely if you like. The tool then uses only the built-in everyday task list.

---

```
Make a tasks.json file for my personal Jira board. Output ONLY valid JSON in one code block, no explanation.

About me:
- Uni, year, degree:
- Courses this semester (with a short name each, like "Database Systems" -> "DB"):
- Projects, startups, freelance, clubs:
- Hobbies and what a normal week looks like:

My current Jira (from the audit command):
<paste audit output here>

Schema:
{
  "profile": "<one line about me>",
  "courses": [{"name": "<full name>", "short": "<short name>"}],
  "projects": [{"name": "<full name>", "short": "<short name>"}],
  "tasks": [["<title>", "q" or "b", "<area>"]],
  "urgent": [["<title>", "q" or "b", "<area>"]]
}
Rules:
- 70 tasks. "q" = quick (under 10 minutes), "b" = bigger. About 40% quick.
- area = one of my course names, one of my project names, or one of:
  chores, errands, tech, social, family, health, self, admin, finance, career.
- About 60% about my courses and projects, 40% my hobbies and routine.
- Titles 3 to 9 words, the way a student would type a to-do. Specific and varied, no duplicates.
- Where my existing epics fit, use the same course or project names they use.
- 12 urgent tasks: things that suddenly have to be done TODAY. A deadline tonight, a quiz moved to tomorrow,
  a bug before a demo, a form closing at 5pm. Say why it is urgent in the title. Same areas as above.
```
