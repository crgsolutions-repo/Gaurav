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
