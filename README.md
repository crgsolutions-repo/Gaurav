# HR Assistant Chatbot

AI-powered HR assistant built with Flask, Supabase, Gemini, HTML, CSS, and JavaScript.

## Current Capabilities

- Employee login with Flask sessions
- Strict HR-scoped Gemini intent parsing
- Attendance punch in and punch out
- Leave balance lookup
- Database-backed multi-step leave workflow
- Leave submission for manager approval
- Manager leave approval and rejection screen
- Conversation logging

## Local Setup

1. Create `.env` from `.env.example`.
2. Fill in `FLASK_SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, and `GEMINI_API_KEY`.
3. Run the SQL in `schema.sql` inside the Supabase SQL editor.
4. Start the app:

```powershell
venv\Scripts\python.exe app.py
```

## Important Security Note

Secrets must stay in `.env`. Do not commit real Supabase or Gemini keys.

If real keys were previously committed or shared, rotate them in the provider dashboards.

## Workflow Behavior

Leave requests are saved as pending first. Leave balance is deducted only when a manager approves the request.

The chatbot is intentionally strict and should only answer company HR-related requests.

## Tests

Run the focused stabilization tests with:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests
```

## Conversational QA

Run the reusable scenario framework with the bundled dataset:

```powershell
venv\Scripts\python.exe stress_test.py --count 25
venv\Scripts\python.exe stress_test.py --count 50
venv\Scripts\python.exe stress_test.py --count 100
venv\Scripts\python.exe stress_test.py --count all
```

The default transport runs Flask in-process against an isolated in-memory QA datastore, so it does not modify Supabase. The default delay is 10 seconds, and the runner never exceeds 10 requests per minute.

To test a dedicated QA deployment:

```powershell
venv\Scripts\python.exe stress_test.py --count 25 --transport http --base-url http://127.0.0.1:5000
```

Set `QA_EMAIL` and `QA_PASSWORD` when the QA deployment requires login.

Results are written to:

* `test_results/results.json`
* `test_results/results.csv`
* `test_results/summary_report.txt`

## HR Policy RAG

The HR copilot can ground situational answers in local policy sources immediately and use Supabase vector search after indexing.

1. Run `rag_schema.sql` in the Supabase SQL editor.
2. Add the server-only service-role key to `.env`:

```env
SUPABASE_SERVICE_ROLE_KEY=replace-with-service-role-key
RAG_ENABLED=true
```

Keep `SUPABASE_KEY` as the anon key. Never expose the service-role key in HTML or browser JavaScript.

3. Install dependencies and index the policy corpus:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe ingest_policies.py --delay 1
```

Use `--dry-run` to validate policy parsing without API or database writes. The demonstration policy sources are under `policies/source`; generated DOCX/PDF handbooks are under `policies/generated`.

Gemini calls log their purpose, model, latency, token counts, and failures. API failures fall back to existing deterministic HR handling instead of blocking the chat indefinitely.

## Daily GitHub Sync

`tools/github_sync.py` stages changed project files, creates a dated commit, and pushes to `origin/main`. It does not stage `.env` or uploads, and it logs failures rather than attempting automatic conflict resolution.

```powershell
venv\Scripts\python.exe tools\github_sync.py --dry-run
venv\Scripts\python.exe tools\github_sync.py
```

For a manual double-click sync, run `tools\\run_github_sync.bat`. It pushes immediately and is not tied to the daily schedule.
