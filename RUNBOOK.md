# Workforce Scheduler — Runbook

Everything you need to start, use, and keep this app running on your own.

---

## 1. After a reboot — start everything

Open a terminal and run these two commands in order:

**Step 1 — Start Ollama** (the local AI engine, needed even if using Claude API):
```
ollama serve
```
Leave this terminal open (or run it in the background).

**Step 2 — Start the web app** (open a second terminal):
```
cd ~/services/ai_workforce_scheduler
python3 -m flask --app app run --port 5050
```
One app, one port, serves every project — no `PROJECT` variable needed anymore.
Leave this terminal open too.

**Step 3 — Open the app in your browser:**
```
http://localhost:5050
```

Log in, then use the **project switcher** at the top of the sidebar to pick which client
you're working in (only shown if your account has access to more than one).

---

## 1b. Working with multiple projects (EON, freenet, FlixBus, ...)

Every project still gets its **own fully separate database and uploads folder** — nothing about
a client's data is ever shared with another — but it's all served from this one running app now.
Switching projects is a dropdown in the sidebar, not a different URL/port.

**Who can access which project** is controlled per person:
- A **global admin** account has superadmin access to every project automatically, including ones
  added later. Good for workforce-management staff who need to see everything.
- Everyone else only has access to the specific project(s) a superadmin has explicitly granted them
  (Users page → Grant Access), with whatever role makes sense per project — e.g. project lead in
  EON, viewer in freenet.

**Adding a brand-new project**: add it to the `PROJECTS` dict near the top of `app.py` (branding,
holidays, initial teams — see the `'eon'` entry as a template), then restart the app. It
auto-provisions an empty database and uploads folder on that restart. Teams, business parameters,
and shift templates start empty/generic — add the client's real ones through Settings and
Employees → Import, same as any fresh engagement. Grant whoever needs it access via the Users page.

---

## 2. Stopping the app

Press `Ctrl + C` in the terminal where the Flask server is running.  
To stop Ollama: press `Ctrl + C` in that terminal too.

---

## 3. The AI Assistant stops working

**If you see "Claude error / authentication":**
- Your Anthropic API key is missing or wrong.
- Check that the file `~/services/ai_workforce_scheduler/.env` exists and contains:
  ```
  ANTHROPIC_API_KEY=sk-ant-...your key here...
  ```
- Restart the Flask server after editing the file.

**If you want to use the local AI instead (no internet needed):**
- Make sure `ollama serve` is running.
- Log in → Settings → Business Parameters → AI section → change `AI Provider` from `anthropic` to `ollama`.
- Model should be `llama3.2`. Warning: responses will be slow on CPU (no GPU on this machine).

---

## 4. The app crashes or won't start

Check the terminal for error messages. Common causes:

| Error | Fix |
|-------|-----|
| `Address already in use` (port 5050) | Run: `fuser -k 5050/tcp` then try again |
| `ModuleNotFoundError` | Run: `pip install -r requirements.txt` |
| `no such table` | The database is missing — run: `python3 -c "from app import app, db; app.app_context().push(); db.create_all()"` |

---

## 5. The database files

Each project has its own database — all of that project's data (employees, schedules, forecasts)
lives in one file:
```
~/services/ai_workforce_scheduler/instance/<project>/workforce.db
```
e.g. `instance/eon/workforce.db`, `instance/freenet/workforce.db`.

Who's allowed to log in and which projects they can access lives separately, shared across all
projects (since a person is the same person regardless of which client they're working on):
```
~/services/ai_workforce_scheduler/instance/identity.db
```

**Back all of these up regularly.** Copy each project's `workforce.db` plus `identity.db` to a USB
drive or another folder. To restore: stop the app, replace the file(s), restart.

---

## 6. User accounts

Manage users at: **http://localhost:5050/users** (superadmin only) — shows everyone with access to
whichever project you're currently switched into.

| Role | What they can do |
|------|-----------------|
| viewer | Read-only — can see everything, change nothing |
| user | Full use: employees, schedules, forecasts, documents, AI chat |
| admin | User + Settings (shifts, parameters, teams) |
| dev | Admin + can change the AI provider in Settings |
| superadmin | Everything + manage user accounts for that project |

Separately, **global admin** (a checkbox on a user, not a role) grants superadmin access to every
project automatically, present and future — for workforce-management staff who need to see
everything without being re-granted access every time a new client is onboarded.

---

## 7. Quick reference — where things are

| Task | Where |
|------|-------|
| Import a new forecast | Forecast → New Forecast Period |
| Import employees from Excel | Employees → Import |
| Generate a schedule | Schedules → Generate |
| Upload a document | Documents → Upload Document |
| Change shift templates | Settings → Shift Templates |
| Change AI provider | Settings → Business Parameters → AI *(dev/superadmin only)* |
| Add/edit users | http://localhost:5050/users *(superadmin only)* |

---

## 8. Files that matter

```
~/services/ai_workforce_scheduler/
  app.py              ← the entire web application, incl. the PROJECTS registry
  .env                ← API keys, shared across all projects (keep this private)
  instance/
    identity.db            ← who can log in and which projects they can access (back this up!)
    eon/workforce.db        ← EON's data (back this up!)
    freenet/workforce.db    ← freenet's data (back this up!)
  uploads/
    eon/                    ← EON's uploaded forecast/spec files and documents
    freenet/                ← freenet's, separately
  scheduler/
    models.py         ← database structure, incl. IdentityUser/ProjectRole and the
                         per-request project-routing (ProjectScopedSession)
    algorithm.py      ← schedule generation logic
    llm.py            ← AI assistant logic
```

---

*Last updated: 12 Jul 2026*
