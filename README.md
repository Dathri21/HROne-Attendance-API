# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the HROne Software Engineer Trainee assignment.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell before starting the app. For local development, a `.env` file can be used but **do not commit it**. The process environment takes precedence over `.env`.

```bash
uvicorn app.main:app --port 8000 --reload
```

The application creates indexes at startup. Check readiness at `http://127.0.0.1:8000/health` and interactive docs at `http://127.0.0.1:8000/docs`.

To load the supplied sample documents into a development database, run `python sample_seed.py`. This deletes/replaces the two sample collections, so use only a disposable development database. The sample data is not enough to validate all edge cases.

## What is implemented

- Employee create/list and attendance punch-in/list.
- Punch-out with shift-aware calculations and atomic update.
- Attendance regularization with derived-field recalculation and an append-only audit trail.
- Monthly employee and department summaries, late leaderboard and department daily trend using MongoDB aggregation pipelines.
- Explain endpoint for the main query/pipeline for each supported analytics/list endpoint.
- Startup indexes, environment configuration and readiness ping.

## Before submission

Run tests against MongoDB 6.0+ and add checks for duplicate concurrent punch-ins, concurrent punch-outs, concurrent corrections, overnight shifts, tie ranks, employees with no attendance logs, weekend records, joiners partway through a month, and trend ranges with missing days. Verify the explain plans on a dataset of realistic size. MongoDB aggregation and explain behaviour depends on the MongoDB server version and indexes, so do not treat successful startup or the nine sample documents as proof that every hidden test passes.

`REVIEW.md` records the starter defects and `DECISIONS.md` explains the key implementation choices. Review these files and make sure you can explain the code yourself before submitting.

No `.env`, secrets, Dockerfile or virtual environment should be committed. 
