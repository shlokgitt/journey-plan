# Delhi bus travel-time model and journey planner

## Run

One command at a time in the VS Code terminal.

```text
python build_model.py
python validation.py
python -m uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

Example API request:

```text
http://127.0.0.1:8000/journeys?from=Rohini&to=Azadpur&depart=2026-10-14T18:30
```

## Process one GPS day

```text
python process_day.py 2026_10_15
```

Do not extract the full 7z archive. The script extracts only one SQLite day.
