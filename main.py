from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

import auth
import cache
import db
import triage
from auth import AuthedUser, AuthError
from llm.schema import TriageRequest
from triage import TriageError

app = FastAPI(
    title="Task API",
    description="A to-do list API with Supabase-backed authentication.",
)
db.init_db()
cache.ping_with_retry()
auth.ping()


@app.exception_handler(AuthError)
def handle_auth_error(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

@app.exception_handler(TriageError)
def handle_triage_error(request: Request, exc: TriageError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


class TaskCreate(BaseModel):
    title: str

class TaskUpdate(BaseModel):
    title: str | None = None
    done: bool | None = None

class SignupRequest(BaseModel):
    # Optional with a None default (not a bare `str`) so a missing field is
    # a normal 400 with a JSON body, not FastAPI's automatic 422.
    email: str | None = None
    password: str | None = None

class LoginRequest(BaseModel):
    email: str | None = None
    password: str | None = None


@app.get("/")
def root():
    return {"name": "Task API", "version": "1.0", "endpoints": ["/tasks", "/tasks/triage"]}

@app.get("/health")
def health():
    """Not just "is the process alive" — actually pings Postgres with
    SELECT 1, and Redis with PING. A load balancer polling this can pull
    an instance out of rotation the moment a dependency goes bad, instead
    of routing traffic to a server that can't serve real requests."""
    db_status = "ok"
    try:
        db.ping()
    except Exception:
        db_status = "error"

    redis_status = "ok"
    try:
        cache.ping()
    except Exception:
        redis_status = "error"

    healthy = db_status == "ok" and redis_status == "ok"
    body = {"status": "ok" if healthy else "degraded", "db": db_status, "redis": redis_status}
    return body if healthy else JSONResponse(status_code=503, content=body)

@app.get("/tasks", summary="List all tasks")
def get_tasks():
    return db.list_tasks()

@app.get("/tasks/{task_id}")
def get_task(task_id: int):
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return row

@app.post("/tasks", status_code=201)
def create_task(task: TaskCreate):
    if not task.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    return db.create_task(task.title)

@app.put("/tasks/{task_id}")
def update_task(task_id: int, update: TaskUpdate):
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Fall back to the existing value for any field the client didn't send.
    new_title = row["title"]
    new_done = row["done"]

    if update.title is not None:
        if not update.title.strip():
            raise HTTPException(status_code=400, detail="title cannot be empty")
        new_title = update.title

    if update.done is not None:
        new_done = update.done

    return db.update_task(task_id, new_title, new_done)

@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int):
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    db.delete_task(task_id)

@app.post("/tasks/triage")
def triage_task(payload: dict = Body(...)):
    """Takes a raw, messy task description and returns a category, a
    priority, and a cleaned-up title — see JOB-CARD.md. The body is taken
    as a plain dict (not a Pydantic route parameter) specifically so a bad
    request lands here, in our own {"error": "field: message"} shape and a
    400, before a single model call is made — instead of FastAPI's default
    422, which is what a Pydantic-typed parameter would produce."""
    try:
        request = TriageRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "text"
        return JSONResponse(status_code=400, content={"error": f"{field}: {first['msg']}"})

    return triage.run_triage(request.text)

@app.post("/auth/signup", status_code=201)
def signup(body: SignupRequest):
    if not body.email or not body.password:
        return JSONResponse(status_code=400, content={"error": "email and password are required"})
    try:
        result = auth.sign_up(body.email, body.password)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    user = result.user
    return JSONResponse(
        status_code=201,
        content={"id": user.id, "email": user.email, "created_at": str(user.created_at)},
    )

@app.post("/auth/login")
def login(body: LoginRequest):
    if not body.email or not body.password:
        return JSONResponse(status_code=400, content={"error": "email and password are required"})
    try:
        result = auth.sign_in(body.email, body.password)
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Invalid login credentials"})
    return {
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
        "token_type": "bearer",
    }

@app.post("/auth/logout", status_code=204)
def logout(user: AuthedUser = Depends(auth.require_user)):
    try:
        auth.sign_out(user.token)
    except Exception:
        pass  # best-effort revoke — see auth.sign_out()'s docstring
    return Response(status_code=204)

@app.get("/public/info")
def public_info():
    return {"message": "Welcome stranger! This info is public."}

@app.get("/protected/profile")
def profile(user: AuthedUser = Depends(auth.require_user)):
    return {"id": user.id, "email": user.email, "created_at": user.created_at}

@app.get("/protected/dashboard")
def dashboard(user: AuthedUser = Depends(auth.require_user)):
    # A second route on the same guard, written with zero new auth code —
    # that reuse is the whole point of Stage 4.
    return {"message": f"Welcome back, {user.email}!"}
