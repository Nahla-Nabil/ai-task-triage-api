# AI Task Triage API

A FastAPI to-do API (PostgreSQL and Redis in Docker Compose, Supabase Auth) with one AI feature built the careful way. `POST /tasks/triage` turns messy text like `pay the electric bill by friday` into a **category, a priority and a clean title**, treating the model as an untrusted component: the input is validated first, the prompt is a versioned file, the output is checked against a schema, a bad answer gets exactly one repair attempt and is then quarantined, and every call has a timeout, a retry policy, a cost log and a kill switch.

On an 8-case hand-labelled eval (including a prompt-injection attempt) it classifies **8/8 correctly** on category. Details are in [AI triage](#ai-triage-post-taskstriage).

## How it fits together

```mermaid
flowchart LR
    client(["Client<br/>curl or Swagger UI"]) -->|"HTTP :8000"| api

    subgraph compose["docker compose up"]
        api["FastAPI app<br/>main.py"]
        db[("PostgreSQL 16<br/>tasks table")]
        redis[("Redis 7")]
        api -->|"parameterised SQL"| db
        api -->|"health check PING"| redis
    end

    api -->|"verify token"| supabase["Supabase Auth"]
    api -->|"POST /tasks/triage"| llm["LLM provider<br/>OpenAI-compatible API"]
```

Postgres and Redis run in containers; Supabase and the model provider are cloud services, so their credentials come from `.env`.

## What happens to one triage request

```mermaid
flowchart TD
    req(["POST /tasks/triage"]) --> valid{"Valid request?<br/>text present, up to 2000 chars"}
    valid -- no --> e400[/"400 naming the field<br/>no model call, no cost"/]
    valid -- yes --> stub{"LLM_STUB=1?"}
    stub -- yes --> stubout[/"200 fixed object<br/>no model call"/]
    stub -- no --> kill{"LLM_ENABLED=false?"}
    kill -- yes --> fallback[/"200 safe fallback<br/>category other, confidence 0.0"/]
    kill -- no --> call1["Call the model<br/>30 s timeout, retry on timeout, 429 and 5xx"]
    call1 -- "provider failed" --> e5xx[/"504 too slow<br/>503 provider error"/]
    call1 -- "got a reply" --> check1{"Valid JSON matching<br/>the TriageResult schema?"}
    check1 -- yes --> ok[/"200 category, priority,<br/>clean_title, confidence"/]
    check1 -- no --> repair["One repair call:<br/>the bad answer plus the exact error"]
    repair --> check2{"Valid now?"}
    check2 -- yes --> ok
    check2 -- no --> quarantine["Save the raw output to<br/>logs/quarantine.jsonl"]
    quarantine --> e422[/"422 no bad object reaches the caller"/]

    classDef good fill:#d1fae5,stroke:#059669,color:#064e3b
    classDef bad fill:#fee2e2,stroke:#e11d48,color:#7f1d1d
    class ok,stubout,fallback good
    class e400,e5xx,e422 bad
```

Every model call writes one JSON log line (prompt version, model, tokens, duration, whether it was a repair), which is what the cost estimate below is built from.

## Project map

| Path | What it is |
| --- | --- |
| `main.py` | FastAPI routes and error handlers |
| `db.py` | The only file with SQL (all queries parameterised) |
| `cache.py` | Redis connection and health ping |
| `auth.py` | Supabase Auth calls and the `require_user` guard |
| `triage.py` | The triage pipeline shown above |
| `llm/` | `client.py` (the only module that talks to a model provider), `schema.py` (the Pydantic input and output contract), `hello.py` (a standalone connectivity check) |
| `prompts/triage-v1.md` | The versioned prompt |
| `evals/` | `cases.json` (8 labelled cases) and `run_eval.py` |
| `JOB-CARD.md` | The spec for the triage feature, including its "must never" list |
| `ai-version/` | AI-generated versions kept for the "AI vs me" comparisons at the bottom; the app doesn't use them |
| `screenshots/` | Images used in this README |
| `compose.yaml`, `Dockerfile` | The one-command run |

## Database

- **Why Postgres in Docker:** the API has now been swapped onto three different storage engines — in-memory (A1), a SQLite file (A2), and now a real Postgres server (A3) — and the routes never changed. That's the point: storage is just an implementation detail behind one small module. Postgres is the same relational engine behind most real production backends, and Docker means no local Postgres install, no version fights — `docker run`/`docker compose up` and a real database server appears on `localhost`.
- **The repository module:** every single line of SQL in this project lives in [`db.py`](db.py) — `main.py`'s routes only ever call `db.list_tasks()`, `db.get_task()`, `db.create_task()`, `db.update_task()`, `db.delete_task()`. Swapping storage again in the future would only touch this one file.
- **Connection & secrets:** the app reads a `DATABASE_URL` connection string from a `.env` file (git-ignored — never committed) via `python-dotenv`. A `.env.example` with placeholder values is committed instead, so anyone cloning the repo knows exactly which variable to set. No password is ever hardcoded in the app code.
- **Schema & seeding:** on startup, `db.init_db()` runs `CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, title TEXT NOT NULL, done BOOLEAN NOT NULL DEFAULT FALSE)`, then seeds the same three example tasks as before — but only if the table is empty, so restarting the app (or the container) never duplicates them. Because `docker compose`'s `depends_on` only waits for the database *container* to start, not for Postgres to finish accepting connections, `init_db()` also retries the first connection a few times before giving up — otherwise the API container could crash on the very first `docker compose up`.
- **Queries:** every query uses **parameterized placeholders** (`%s`, values passed separately via `psycopg`) for every `SELECT`/`INSERT`/`UPDATE`/`DELETE` — never string-formatted SQL.
- **Persistence:** Postgres's data directory is mounted to a named Docker **volume** (`taskdata`), so tasks survive a full `docker compose down` + `docker compose up` — verified by creating a task, tearing the whole stack down, bringing it back up, and confirming the task was still there.

### Explored with psql

Opened a SQL prompt directly inside the database container and ran a query by hand:

```sql
docker exec -it <db-container-name> psql -U postgres -d tasks -c "\dt"
docker exec -it <db-container-name> psql -U postgres -d tasks -c "SELECT * FROM tasks;"
```

![psql — tasks table](screenshots/postgres-tasks.png)

## Auth

- **Why Supabase Auth:** the golden rule for this stage is that this project never hashes a password or signs a token itself — that's exactly the kind of thing that ends careers when rolled by hand. Supabase is the **Identity Provider**: it stores accounts, hashes passwords, and issues signed JWTs. This app's job is only the backend-developer part — receiving a token, verifying it with Supabase, and opening (or refusing) the door.
- **The auth module:** every call into the Supabase SDK lives in [`auth.py`](auth.py) — same shape as `db.py` and `cache.py`. It builds a **fresh Supabase client per call** instead of holding one shared global, which matters more than it looks: the Python SDK's `sign_in`/`sign_out` calls store session state *on the client object*, so a shared client would leak one user's session into another user's concurrent request.
- **The guard:** `auth.require_user` is a single reusable FastAPI dependency — `Depends(auth.require_user)` — applied to `GET /protected/profile`, `GET /protected/dashboard`, and `POST /auth/logout`. It's built on FastAPI's `HTTPBearer` security scheme, which is also what makes the Swagger "Authorize" padlock appear on those routes automatically.
- **Error shape:** every auth failure (missing token, malformed header, expired/tampered token, missing signup/login fields, bad credentials) responds with `{"error": "..."}` and the exact status code the assignment specifies — via a custom `AuthError` exception and an `@app.exception_handler` in `main.py`, rather than FastAPI's default `{"detail": "..."}` shape used elsewhere in this API.
- **Logout is best-effort.** JWTs are stateless — nothing server-side can "un-sign" an access token, so it stays valid until it naturally expires (Supabase's default is one hour) no matter what `/auth/logout` does. The route revokes the refresh token/session with Supabase, but that's why it still returns `204` even if that revocation call itself fails: the token was already verified valid by the guard before the route ran.
- **Secrets:** `SUPABASE_URL` and `SUPABASE_KEY` (the **anon key** — never the `service_role` key, which bypasses all security) are read from `.env` the same way `DATABASE_URL` and `REDIS_URL` already were.
- **Scope, on purpose:** the existing `/tasks` routes are intentionally left untouched and unprotected this round. This assignment is scoped to exactly five new routes (`/auth/*`, `/protected/*`, `/public/info`) to practice the auth pattern on its own — wiring task ownership onto `/tasks` is explicitly next week's tenant-isolation work.

## AI triage (`POST /tasks/triage`)

**What it does:** takes a raw, messy task description — the kind you type in ten seconds without thinking about it — and returns a category, a priority, and a cleaned-up title, so it lands in the right place on the list without you doing the sorting by hand. See [JOB-CARD.md](JOB-CARD.md) for the full spec, including the "must never" list and the when-unsure rule.

- **One narrow job, not a chatbot.** One request in `{"text": "..."}`, one structured answer out. `category` and `priority` are drawn from fixed lists (`work|personal|shopping|health|other`, `low|normal|high`) — never free text, never invented.
- **The contract exists before the model does.** [`llm/schema.py`](llm/schema.py) defines `TriageRequest` (input) and `TriageResult` (output) as Pydantic models. Every request is validated against `TriageRequest` in [`main.py`](main.py) *before* any model call — a missing, empty, too-long, or wrong-typed `text` field returns `400` naming the field, and costs nothing.
- **The prompt is a versioned file**, not a string in a route handler: [`prompts/triage-v1.md`](prompts/triage-v1.md). It has a role, the exact output shape, the closed lists, the "never" rules, the when-unsure instruction, and three worked examples (a typical case, an ambiguous one, and a prompt-injection attempt). The task text always travels as its own JSON-encoded user message — never concatenated into the system prompt — which is the cheap, standard defence against prompt injection described in [OWASP LLM01](https://owasp.org/www-project-top-10-for-large-language-model-applications/).
- **Parse → validate → repair once → quarantine.** [`triage.py`](triage.py) strips any code fence the model wraps its answer in, parses the JSON, and validates it against `TriageResult`. If that fails (bad JSON, or a value outside a closed list), it makes exactly one repair call — the model's own broken answer plus the exact validation error, asking for a corrected object — and if that *also* fails, the request gets a `422` and the raw output is appended to `logs/quarantine.jsonl` (git-ignored — it can contain real user task text) instead of the process crashing or a bad object reaching a caller. Raw model text is never returned to the caller, success or failure.
- **A real timeout, and a retry policy that knows when to stop.** [`llm/client.py`](llm/client.py) sets an explicit 30-second timeout (`LLM_TIMEOUT_SECONDS`) and disables the OpenAI SDK's own defaults — a 10-minute timeout and 2 automatic retries — in favour of its own explicit policy: retry on a timeout, `429`, or `5xx` (exponential backoff with jitter, capped at `LLM_MAX_RETRIES=2` extra attempts; a `Retry-After` header is obeyed instead of guessed), and **never** retry a `400`, `401`, or `403` — a bad key stays a bad key. A timeout that survives the retries returns `504`; a provider-side auth/permission/status error returns `503`. None of it takes the process down.
- **Every call is logged.** One structured JSON line to stdout per model call: prompt version, model, input/output token counts, duration, and whether it was a repair — the same line the cost estimate below is built from.
- **A kill switch.** `LLM_ENABLED=false` skips the model entirely and returns a safe, deterministic fallback (`category: "other"`, `confidence: 0.0`) instead — no deploy required to turn the feature off during a provider outage or a bill spike.
- **A stub mode**, separate from the kill switch: `LLM_STUB=1` also skips the model, returning a fixed schema-valid object — used for every restart-the-server iteration while building, so debugging never spends a real call. The repo's own `.env` ships with `LLM_STUB=1` by default so cloning and running it costs nothing until you deliberately turn it off.

**Provider:** [OpenRouter](https://openrouter.ai) (hosted, free, no credit card), model `openrouter/free`. Swapping to a different OpenRouter model, or to a local [Ollama](https://ollama.com) server, is changing exactly three env vars and nothing else — `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — because every line that knows about the provider lives in `llm/client.py`. That's also why the client library is the package literally named `openai` even though the default provider here isn't OpenAI: it's the request shape almost every provider has since copied.

**Setup, once, before it costs anything:** at [openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy), turn **on** both *"Free endpoints that may train on request data"* and *"Free endpoints that may publish prompts"* — free models 404 until both are on. Because of that setting, never send real personal data through this endpoint; the eval cases below are all made up.

```bash
curl.exe -i -X POST http://localhost:8000/tasks/triage -H "Content-Type: application/json" -d "{`"text`":`"pay the electric bill by friday`"}"
```

Response:
```json
{
  "category": "personal",
  "priority": "high",
  "clean_title": "Pay electric bill",
  "confidence": 0.9
}
```

A deliberately broken request — text over the 2000-character limit, or missing entirely:
```bash
curl.exe -i -X POST http://localhost:8000/tasks/triage -H "Content-Type: application/json" -d "{}"
```

Response:
```json
{ "error": "text: Field required" }
```

**Eval:** `evals/cases.json` has 8 hand-labelled cases (5 typical, 1 ambiguous, 1 near-empty, 1 a prompt-injection attempt), run by `python evals/run_eval.py` against a live server.

> **8/8 correct on category** — `prompts/triage-v1.md`, run 2026-09-09 against `openrouter/free` on a real OpenRouter key, `LLM_STUB=0`. Zero repairs needed on any of the 8 calls (`repaired: false` on every logged line) and `logs/quarantine.jsonl` stayed empty — the prompt held on the first try every time, including the prompt-injection case (classified as `other`, never followed the embedded instruction). The two when-unsure cases (`vague`, `empty_ish`) both landed on `category: "other"` with confidence `0.3`, exactly as `prompts/triage-v1.md`'s when-unsure rule asks for.

**Cost:** one `/tasks/triage` call on `openrouter/free` logs its exact token counts to stdout (see `_log_cost` in `triage.py`). Real numbers from the eval run above (9 calls, including one manual check): **568 input / 640 output tokens/call on average** — output is unusually high because this particular free router happens to land on a reasoning-style model that thinks out loud before the JSON (`parse_json_object`'s brace-slicing is what makes that safe to ignore). Free-tier requests are $0 regardless of token count. For a paid model at 10,000 requests/day, that's roughly 5.7M input + 6.4M output tokens/day — e.g. ~$0.80/day on a cheap small model (~$0.05/M in, $0.08/M out) up to ~$4.70/day on something like GPT-4o-mini ($0.15/M in, $0.60/M out); the [LLM price calculator](https://llmpricecheck.com/calculator/) does the exact arithmetic for a specific model. **Output tokens are the biggest cost driver here** — the opposite of the "input always dominates" rule of thumb — specifically because of that reasoning overhead.

**What I'd fix with another day:** pin `openrouter/free` to a non-reasoning small model instead of the router default — the 640-token average output above is almost entirely invisible "thinking" the caller never sees, and it's most of the per-call latency (several calls took 10-30+ seconds) and cost. After that: cache identical `{text, prompt_version}` requests — task text repeats more than free-form support messages do (the same three or four chores get re-typed) — and add `response_format` / structured-output support where the provider allows it, so a malformed answer becomes impossible instead of merely unlikely and repaired.

## How to run

**The one-command way (recommended):**

1. Clone this repo and enter the folder:
```bash
   git clone https://github.com/Nahla-Nabil/ai-task-triage-api.git
   cd ai-task-triage-api
```
2. Copy the example env file, then fill in `SUPABASE_URL` and `SUPABASE_KEY` from your own Supabase project (`Project Settings → API` — use the **anon** key, never `service_role`). `compose.yaml` sets its own `DATABASE_URL`/`REDIS_URL` for the containerized run, but Supabase and the LLM provider are both cloud services, not containers in this stack, so those variables always come from `.env`:
```bash
   cp .env.example .env
```
   `LLM_STUB=1` is the default in `.env.example`, so the app runs with zero AI setup out of the box. To see `POST /tasks/triage` call a real model: sign up at [openrouter.ai](https://openrouter.ai), turn on both privacy toggles at [openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy) (free models 404 until you do), create a key, and set `LLM_API_KEY` in `.env` — then set `LLM_STUB=0`.
3. Start everything — the API, Postgres, and Redis:
```bash
   docker compose up --build
```
4. Open `http://localhost:8000` in your browser. The `tasks` table and three example tasks are created automatically on first run.

**Running locally against a standalone Postgres container** (useful while developing without rebuilding the image each time):

1. Start Postgres by itself, with a volume so data persists:
```bash
   docker run --name taskdb -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=tasks -p 5433:5432 -v taskdata:/var/lib/postgresql/data -d postgres:16
```
   (Host port `5433` is used instead of the default `5432` here because a native Postgres install can already be sitting on `5432` on some machines — adjust to `5432` if that's free on yours, and update `.env`/`.env.example` to match.)
2. Create a virtual environment and install dependencies:
```bash
   python3 -m venv venv
   venv\Scripts\Activate.ps1   # Windows PowerShell
   pip install -r requirements.txt
```
3. Make sure `.env` has `DATABASE_URL=postgresql://postgres:dev@localhost:5433/tasks`, then start the server:
```bash
   uvicorn main:app --reload --port 8000
```

## Endpoints

| Method | Path                  | Description                              | Auth required?              |
|--------|-----------------------|-------------------------------------------|------------------------------|
| GET    | /                     | API info                                 | No                           |
| GET    | /health               | Health check                             | No                           |
| GET    | /tasks                | List all tasks                            | No                           |
| GET    | /tasks/{id}           | Get a single task                         | No                           |
| POST   | /tasks                | Create a new task                          | No                           |
| PUT    | /tasks/{id}           | Update a task                             | No                           |
| DELETE | /tasks/{id}           | Delete a task                             | No                           |
| POST   | /tasks/triage         | AI: category, priority & clean title      | No                           |
| POST   | /auth/signup          | Create a new user account                 | No                           |
| POST   | /auth/login           | Authenticate & return a JWT               | No                           |
| POST   | /auth/logout          | End the user's session                    | Yes — `Authorization: Bearer <token>` |
| GET    | /protected/profile    | Read the logged-in user's profile         | Yes — `Authorization: Bearer <token>` |
| GET    | /protected/dashboard  | A second route on the same auth guard     | Yes — `Authorization: Bearer <token>` |
| GET    | /public/info          | Read public, open data                    | No                           |

`/tasks/*` predates this assignment and is intentionally still open — see [Auth](#auth) above for why.

## Example request

```bash
curl.exe -i -X POST http://localhost:8000/tasks -H "Content-Type: application/json" -d "{`"title`":`"Read a book`"}"
```

Response:
```json
{
  "id": 4,
  "title": "Read a book",
  "done": false
}
```

## Swagger UI

Interactive docs are available at `http://localhost:8000/docs`.

![Swagger UI screenshot](screenshots/swagger-screenshot.png)

`/auth/logout`, `/protected/profile`, and `/protected/dashboard` show a lock icon. Click **Authorize**, paste an `access_token` from `POST /auth/login`, then **Try it out** → **Execute** on `/protected/profile` works straight from the browser — no curl needed:

![Swagger UI — authorized bearer request](screenshots/swagger-auth-screenshot.png)

## Extras

A few optional stretch goals from the assignment, done after the core 6 stages:

**A real health check.** `GET /health` doesn't just report "the process is alive" — it runs `SELECT 1` against Postgres via `db.ping()` and `PING` against Redis via `cache.ping()`. If either fails, it returns `503` with `{"status": "degraded", ...}` instead of a plain `200`. This matters because a load balancer polling `/health` can pull an instance out of rotation the moment a dependency goes bad, instead of continuing to route real traffic to a server that can't actually serve it.

**Redis, added to the stack.** A third service (`redis:7-alpine`) now runs alongside the app and Postgres in `compose.yaml`, not doing anything yet — it's not used by any endpoint — but wired up and pinged on startup (`cache.ping_with_retry()` in `main.py`, same retry-until-ready reasoning as `db.init_db()`) so week 4's caching work has a warm connection to build on instead of starting from zero. `GET /health` reports `redis: "ok"` alongside `db: "ok"` once it's confirmed reachable.
```json
{"status": "ok", "db": "ok", "redis": "ok"}
```

**An index on `tasks.done`, and a genuinely surprising `EXPLAIN ANALYZE`.** Bulk-seeded the table with 200,000 extra rows to make a difference measurable, then compared `EXPLAIN ANALYZE SELECT * FROM tasks WHERE done = true`:

| Step | Plan | Execution time |
|---|---|---|
| Before the index | Seq Scan | 8.78 ms |
| After `CREATE INDEX`, before `ANALYZE` | **Still Seq Scan** | 8.72 ms |
| After running `ANALYZE tasks` | Bitmap Index Scan | **4.48 ms** |

Creating the index alone didn't change anything — Postgres's query planner was still working off stale statistics from before the index existed, so it kept picking a sequential scan. Only after `ANALYZE` refreshed those statistics did the planner realize the index was worth using, roughly halving execution time. The index (`idx_tasks_done`) is now created automatically in `db.init_db()`; the 200k test rows were deleted afterward — the seeded table still only has the original 3 tasks.

**A multi-stage Dockerfile.** Split the build into a `builder` stage that installs dependencies into `/install`, and a final stage that only copies that installed prefix plus the app source (`main.py`, `db.py`, `cache.py`, `auth.py`, `triage.py` and the `llm/` and `prompts/` folders) — no pip cache, build metadata, or intermediate layers carried into the final image. Went from **240MB → 227MB** (measured back when only `main.py` and `db.py` were copied; the image now also ships the AI code, so today's exact size differs). The reduction is modest here since the original Dockerfile already used `--no-cache-dir` and never needed extra build tools (`psycopg[binary]` ships prebuilt wheels) — the main win of multi-stage builds shows up more when a project actually needs a compiler toolchain to build dependencies.

## AI vs me — Assignment 1 (in-memory API)

**Prompt used:**
> Build this inside the ai-version/ folder only, don't touch main.py: Build a REST API using Python and FastAPI. It needs these 5 endpoints: GET /tasks, GET /tasks/{id}, POST /tasks, PUT /tasks/{id}, and DELETE /tasks/{id}. When creating a task, return status code 201. When deleting, return status code 204. If the title is empty, return status code 400 with an error message. If a task id doesn't exist, return status code 404. Store the tasks in memory, no database needed. Also add Swagger UI documentation.

**What the AI did better:**
The AI used Pydantic's `response_model` (a `Task` schema) on every endpoint instead of returning plain dicts, which makes the Swagger docs show the exact response shape. It also factored out a shared `find_task()` helper instead of repeating the same lookup loop in every endpoint — cleaner and less repetitive than my version.

**What it got wrong or ignored:**
My prompt never mentioned `GET /` or `GET /health`, so the AI's version doesn't have them at all — it only built exactly the 5 endpoints I listed, nothing more. It also worded the validation error message differently ("title cannot be empty" vs. my "title is required") since I never specified exact wording.

**What my prompt forgot to specify — and what the AI silently decided:**
I didn't specify how new task IDs should be generated, so the AI used a global `next_id` counter starting at 4, while I had computed `max(id) + 1` dynamically. Both work, but they'd behave differently if a task were ever deleted and a new one created after. I also never asked for a Field description or an app title/description in the FastAPI() constructor — the AI added those on its own for nicer-looking docs.

**One rematch:**
I'd improve the prompt by explicitly asking for `GET /` and `/health` endpoints, and specifying that new IDs should reuse the `max(existing_ids) + 1` logic to match my original behavior exactly.

## AI vs me — Assignment 2 (SQLite migration, Stage 6)

**Prompt used:**
> Migrate the API in ai-version/main.py (the AI version from Assignment 1) from the in-memory list to SQLite. Keep everything inside ai-version/ — don't touch the root main.py. Use Python's built-in sqlite3 module (no ORM) and a file called tasks.db. On startup, create a `tasks` table if it doesn't already exist, with columns id (integer primary key), title (text), and done (boolean). Seed three example tasks — "Buy milk" (not done), "Walk the dog" (not done), "Learn FastAPI" (done) — but only if the table is empty, so restarting the app never duplicates them. Every endpoint must keep exactly the same behaviour as before: GET /tasks, GET /tasks/{id}, POST /tasks (201, or 400 if title is missing/empty), PUT /tasks/{id} (200, partial updates allowed, 404 if the id doesn't exist), DELETE /tasks/{id} (204, or 404 if the id doesn't exist). All queries must use parameterized placeholders (?) — never build SQL by pasting values into the string.

**What the AI did better:**
For `PUT`, the AI used a single `UPDATE tasks SET title = COALESCE(?, title), done = COALESCE(?, done) WHERE id = ?` query and read `cursor.rowcount` to detect a missing id. My version does a `SELECT` first to check the row exists, then a separate `UPDATE` — the AI's approach is one round trip to the database instead of two, for the same result.

**What it got wrong or ignored:**
The AI validated the create-task title only with Pydantic's `Field(..., min_length=1)`, no manual `.strip()` check. `min_length=1` blocks `""` but not whitespace like `"   "` — so `POST /tasks` with `{"title": "   "}` returned **201 Created** with a blank-looking task instead of the `400` my prompt asked for. I confirmed this by actually running the endpoint (see below); it's exactly the kind of edge case that's easy to miss when you only skim generated code instead of testing it. Interestingly, the AI *did* add a correct `.strip()` check on the `PUT` endpoint — so the same validation rule was enforced inconsistently between two endpoints of the same file.

```
$ curl -X POST /tasks -d '{"title": "   "}'
→ before fix: 201 Created  {"id": 5, "title": "   ", "done": false}
→ after fix:  400 Bad Request  {"detail": "Title cannot be empty"}
```

**What my prompt forgot to specify — and what the AI silently decided:**
I never said whether `done` should be stored as `BOOLEAN` or `INTEGER` in the `CREATE TABLE` statement — SQLite doesn't actually have a boolean type (it stores `0`/`1` either way), so the AI picked `INTEGER NOT NULL DEFAULT 0`, which is arguably the more accurate column type since it names what SQLite actually stores. I also never said anything about response docs, and the AI added a `Task` Pydantic model, a `FastAPI(title=..., description=..., version=...)` constructor, and `summary=` text on every route purely for nicer-looking `/docs` output — none of which I asked for.

**One rematch:**
I added one sentence to the prompt — *"reject titles that are empty or contain only whitespace on both POST and PUT, not just missing"* — regenerated the `create_task` validation, and it now correctly returns `400` for a whitespace-only title (shown above), matching `PUT`'s behavior.

## AI vs me — Assignment 3 (Postgres/Docker migration)

**Prompt used:**
> Containerize main.py — the hand-written Python/FastAPI version — onto Postgres, using psycopg. Everything lives in a new ai-version/ folder. Don't touch the root main.py or db.py. Startup connects to Postgres using a DATABASE_URL from .env — password never hardcoded. Create the tasks table if it's not there yet: id serial primary key, title text, done boolean. Seed the same three tasks as before ("Buy milk" and "Walk the dog" not done, "Learn FastAPI" done), but only when the table's empty — restarting the app shouldn't add them again. Endpoints need to behave exactly like they do now: GET /tasks, GET /tasks/{id} (404 if missing), POST /tasks (201, or 400 for missing/empty title), PUT /tasks/{id} (200, partial updates fine, 404 if the id's not there), DELETE /tasks/{id} (204, or 404 if missing). Queries stay parameterized with %s placeholders — no pasting values into SQL strings. Write a Dockerfile and docker-compose.yml that bring up the app and Postgres together with a named volume, so data survives a restart.

**What the AI did better:**
Same trick as Assignment 2's AI: `PUT` uses a single `UPDATE ... SET title = COALESCE(%s, title), done = COALESCE(%s, done) ... RETURNING *`, and `DELETE` reads `cursor.rowcount` instead of doing a `SELECT` first — one round trip instead of two, for both. It also added `response_model=Task` on every route for nicer `/docs` output, which I didn't ask for.

**What it got wrong or ignored:**
1. **The exact same whitespace-title bug as Assignment 2, again.** `POST /tasks` only validates with `Field(..., min_length=1)`, no `.strip()` — so `{"title": "   "}` returns `201 Created` instead of `400`. `PUT` gets a correct manual `.strip()` check, so the same inconsistency between the two endpoints repeated itself almost exactly, in a brand-new file, on a completely different prompt.
```
$ curl -X POST /tasks -d '{"title": "   "}'
→ 201 Created  {"id": 4, "title": "   ", "done": false}
```
2. **`docker-compose.yml` used an obsolete `version: "3.9"` key** — Compose printed a deprecation warning on every `up`/`down`. My own `compose.yaml` doesn't have this line at all.
3. **No connection-retry logic at startup.** My `db.init_db()` retries the first Postgres connection a few times, because `depends_on` only waits for the *container* to start, not for Postgres itself to finish accepting connections. The AI's `init_db()` makes one bare connection attempt — it happened to work here because the Postgres image was already cached and started fast, but that's luck, not a guarantee; a slower first pull could crash it on the very first `docker compose up`.
4. **Defaulted to host port 8000 with no awareness of my machine.** My own stack was already running on 8000, so `docker compose up` failed outright the first time (`port is already allocated`) until I stopped my own stack to test the AI's. Not really the AI's fault — it can't know what's already running locally — but it's a reminder that "works first try" depends on context an AI never has.

**What my prompt forgot to specify — and what the AI silently decided:**
I never said what the Postgres username/password should default to, so the AI picked `postgres`/`postgres` — different from my own `dev` default, so the two `.env.example` files don't actually match each other even though they solve the same problem. I also never mentioned `GET /` or `/health` (only the 5 CRUD routes), so — exactly like Assignment 1 — the AI built only what I explicitly listed and nothing more. And I never said whether the DB code should live in its own module: the AI put everything straight into `main.py`, while I split mine into `db.py` — my prompt never actually asked for a "repository module" the way the real assignment brief did.

**One rematch:**
I'd add: *"reject titles that are empty or whitespace-only on both POST and PUT, not just missing — and retry the first database connection a few times at startup, since the app container may start before Postgres is ready to accept connections."*

## AI vs me — Assignment 4 (Auth)

**Prompt used:**
> Build a FastAPI app in Python that adds Supabase-based authentication to a small REST API. Use the `supabase` PyPI package as the Supabase client, initialized from SUPABASE_URL and SUPABASE_KEY environment variables loaded via python-dotenv. Implement exactly these five routes: POST /auth/signup (body {email, password}, calls supabase.auth.sign_up(), 201 with the created user's id/email, or 400 with a JSON error if a field is missing); POST /auth/login (calls supabase.auth.sign_in_with_password(), 200 with access_token and refresh_token, 400 for missing fields, or 401 with {"error": "Invalid login credentials"} if Supabase rejects it); POST /auth/logout (protected, calls supabase.auth.sign_out(), 204 on success); GET /protected/profile (protected, verifies the bearer token with supabase.auth.get_user(token), 200 with id/email/created_at, or 401 with {"error": "Access token required"} / {"error": "Invalid or expired token"}); GET /public/info (no auth, 200 with a welcome message). Factor the token-verification logic into a single reusable FastAPI dependency applied to every protected route. Configure FastAPI's HTTPBearer security scheme so Swagger UI shows an Authorize padlock on the protected routes. Put everything in one self-contained app inside its own folder, not referencing any other project's files.

Generated in quarantine, via a fresh agent given only this prompt text — no access to my `auth.py`/`main.py` — into [`ai-version/auth/`](ai-version/auth/main.py), so the comparison below is a genuine independent diff, not a paraphrase of my own code.

**How it handled token extraction:** correctly. It used the same `fastapi.security.HTTPBearer(auto_error=False)` primitive I did, so both versions parse the `Authorization: Bearer <token>` header identically — a malformed header (no `Bearer` prefix, or the prefix with no token) lands as `credentials=None` in both, and both return the spec's `401 {"error": "Access token required"}` rather than crashing or falling through to FastAPI's generic 403. Verified live: `curl -H "Authorization: sometoken"` against the AI's `/protected/profile` returns exactly that 401, same as mine.

**Security flaws it introduced:** one real one. It builds a single **module-level** Supabase client (`supabase: Client = create_client(...)`) and reuses it for every request, including inside `/auth/logout`'s bare `supabase.auth.sign_out()` call. The Python SDK's `sign_out()` doesn't take the caller's token as an argument — it operates on whatever session is cached *on the client object itself*. Since nothing in the AI's `get_current_user()` dependency ever calls `set_session()` on that shared client, `sign_out()` isn't actually scoped to the user who just authenticated with the bearer token in their header — under concurrent requests from different logged-in users, that one process-wide client's session state can be whatever the last `sign_in_with_password()` call happened to leave behind, or empty entirely, making `/auth/logout` silently a no-op. My own `auth.py` avoids this by building a **fresh client per call** (`get_client()`), the same "no shared, mutable connection" convention `db.py` and `cache.py` already used in this repo before this assignment even started — a convention the AI, prompted cold with no view of those files, had no way to know about. Elsewhere it handled invalid/expired tokens safely (`get_user()` wrapped in try/except, `None` user → 401) and never referenced `service_role` or logged a raw token, so the flaw is narrow but real.

**What my prompt forgot to specify — and what the AI silently decided:** I never said whether the Supabase client should be constructed once or per-request — the AI defaulted to the single-global-client pattern most tutorials show, which is exactly the choice that causes the `sign_out()` gap above. I also only listed the five official routes, so — matching every earlier "AI vs me" section in this repo — it built precisely those and nothing more, no equivalent of my own extra `/protected/dashboard` demo route. And I never pinned the exact validation-error wording: I return `"email and password are required"`, the AI returns `"Email and password are required"` — cosmetically different, functionally identical. One place it was arguably better than me: it returns `user.created_at` straight through instead of wrapping it in `str()` — FastAPI's encoder already serializes `datetime` to ISO 8601 on its own, so my manual `str()` call in `auth.py` is unnecessary defensive code the AI didn't feel a need for.

**One rematch:** I added one sentence — *"build a fresh Supabase client per request rather than one shared global client, since sign_in/sign_out store session state on the client object"* — which is precisely the fix: regenerating with that constraint should collapse the AI's implementation onto the same per-call `get_client()` shape my hand-written `auth.py` already uses, closing the logout-scoping gap.