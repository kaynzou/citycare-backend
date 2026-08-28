# CityCare Backend

FastAPI service backing the CityCare app — stores complaints, handles photo
uploads, and translates regional-language complaints.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/docs` for interactive API docs (auto-generated
by FastAPI — good for testing endpoints without the frontend).

## Endpoints

| Method | Path                              | Matches frontend call            |
|--------|------------------------------------|-----------------------------------|
| POST   | `/api/complaints`                 | Raise a Complaint → submit        |
| GET    | `/api/complaints`                 | (list/admin view — optional)      |
| GET    | `/api/complaints/{complaint_no}`  | Agent Verification → load page    |
| PATCH  | `/api/complaints/{complaint_no}/status` | Agent Verification → Update Status |

`complaint_no` is the ID without the `#`, e.g. `CMPI1234`.

## Connecting the frontend

In `CityCarePages.jsx`, set:
```js
const API_BASE = "http://localhost:8000/api";
```
And you'll also need CORS-safe image loading — the mounted `/uploads` path
means photo URLs come back as `http://localhost:8000/uploads/CMPI1234/xyz.jpg`,
which the `<img>` tags in the frontend will load directly.

## Known limitation — read before demo day

`langdetect` (used for `detected_language`) is built for native-script text.
Romanized/Hinglish complaints (e.g. "Bahut bada gadda hai" typed in Latin
letters instead of Devanagari) get misdetected often — it guessed Somali in
testing. Two ways to handle this before SIH judging:

1. **Cheap fix**: don't display `detected_language` prominently in the demo,
   or hardcode/skip it for the pitch.
2. **Real fix**: swap `translate_text()` in `translation.py` for the
   commented-out LLM-based version at the bottom of that file. An LLM
   (Claude/GPT) handles code-mixed and romanized Indian languages far
   better than langdetect + Google Translate, because it reasons about
   context instead of pattern-matching script.

## Translation network dependency

`deep_translator` calls Google Translate over the network. If that's
blocked (offline demo, restrictive wifi), `translate_text()` silently
falls back to returning the original text untranslated rather than
failing the whole request — a complaint should always save even if
translation doesn't work. Test on the actual venue wifi beforehand if
you can.

## Swapping SQLite for Postgres

For the hackathon demo, SQLite (`citycare.db`, created automatically) is
fine and needs zero setup. To move to Postgres later, change
`DATABASE_URL` in `app/database.py` and add `psycopg2-binary` to
requirements.
