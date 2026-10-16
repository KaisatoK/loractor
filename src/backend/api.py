import json
import logging
import os
import threading

from collections.abc import AsyncIterable, Iterator
from contextlib import asynccontextmanager
from typing import Literal, TypedDict, cast

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.stream.transformers import CustomTransformer
from pydantic import BaseModel, Field

from backend import jobs
from backend.appdb import auth, chats
from backend.appdb.auth import require_user
from backend.db.auto_complete import auto_complete
from backend.observability import RequestContextMiddleware, metrics
from backend.pipeline.agents import generate_answer
from backend.pipeline.answers import serialize_answer
from backend.pipeline.cost import investigation_cost
from backend.pipeline.graph import chatbot, prompts
from backend.pipeline.roles import ROLE_OBJECTIVES
from backend.pipeline.state import PipelineState, RoleName

logger = logging.getLogger(__name__)

# --- Concurrency limits -------------------------------------------------------
# Sync endpoints run blocking graph/LLM/DB work on the anyio worker pool, keeping
# the event loop free. These caps bound how many investigations run at once.
MAX_THREADS = int(os.getenv("MAX_THREADS", "100"))
MAX_GLOBAL_INFLIGHT = int(os.getenv("MAX_GLOBAL_INFLIGHT", "32"))
MAX_INFLIGHT_PER_USER = int(os.getenv("MAX_INFLIGHT_PER_USER", "2"))

global_slots = threading.BoundedSemaphore(MAX_GLOBAL_INFLIGHT)
user_slots: dict[str, int] = {}
slots_lock = threading.Lock()

# Serialises "check for an active run -> persist the user turn -> launch" so a
# double send cannot persist two user turns or leak a slot. Investigations start
# rarely and quickly, so one lock (not one per chat) is plenty.
_investigation_start_lock = threading.Lock()


def acquire_slot(user_id: str) -> None:
    """Reserve capacity for one investigation, or reject with a clear status."""
    with slots_lock:
        if user_slots.get(user_id, 0) >= MAX_INFLIGHT_PER_USER:
            metrics.inc("chat_streams_rejected_per_user")
            raise HTTPException(
                status_code=429,
                detail=(
                    "Too many concurrent investigations for this user. "
                    "Please wait for one to finish."
                ),
                headers={"Retry-After": "5"},
            )
        if not global_slots.acquire(blocking=False):
            metrics.inc("chat_streams_rejected_global")
            raise HTTPException(
                status_code=503,
                detail="The server is at capacity. Please retry shortly.",
                headers={"Retry-After": "5"},
            )
        user_slots[user_id] = user_slots.get(user_id, 0) + 1


def release_slot(user_id: str) -> None:
    with slots_lock:
        remaining = user_slots.get(user_id, 0) - 1
        if remaining > 0:
            user_slots[user_id] = remaining
        else:
            user_slots.pop(user_id, None)
    global_slots.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Raise the anyio worker-pool ceiling so many streams can be in flight.
    # Must be done here: the limiter only exists inside a running event loop.
    from anyio import to_thread

    to_thread.current_default_thread_limiter().total_tokens = MAX_THREADS
    try:
        auth.purge_expired_sessions()
    except Exception:  # pragma: no cover - startup hygiene, never fatal
        logger.warning("Could not purge expired sessions", exc_info=True)
    logger.info(
        "Concurrency configured: threads=%s global=%s per_user=%s",
        MAX_THREADS,
        MAX_GLOBAL_INFLIGHT,
        MAX_INFLIGHT_PER_USER,
    )
    yield


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatRequest(TypedDict):
    chat_id: str
    role: RoleName
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    history: list[ChatMessage]


class ChatResponse(TypedDict):
    type: str
    content: str


class ReframeRequest(TypedDict):
    target_role: RoleName
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    question: str
    previous_answer: str
    tool_results: list


class InvestigateRequest(TypedDict):
    role: RoleName
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    question: str
    history: list[ChatMessage]


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)


def _render_conversation(history: list[ChatMessage]) -> str:
    """Flatten the chat history into the role-prefixed transcript the agents read."""
    return "\n".join(f"{item['role']}: {item['content']}" for item in history)


def _latest(history: list[ChatMessage], role: str) -> str:
    return next(
        (item["content"] for item in reversed(history) if item["role"] == role), ""
    )


def _thread_id(user_id: str, chat_id: str) -> str:
    """Stable per-chat LangGraph thread key, built from the authenticated user."""
    return f"{user_id}:{chat_id}"


def stream_events(req: ChatRequest, user_id: str) -> Iterator[str]:
    """Run the pipeline and yield newline-delimited JSON events.
    A *synchronous* generator by design: FastAPI iterates it on the worker pool,
    so the blocking graph execution never blocks the event loop."""
    history = req["history"]

    initial_state = cast(
        PipelineState,
        {
            "conversation": _render_conversation(history),
            "messages": [
                {"role": item["role"], "content": item["content"]}
                for item in history
            ],
            "latest_message": _latest(history, "user"),
            "latest_answer": _latest(history, "assistant"),
            "role": {
                "name": req["role"],
                "objective": ROLE_OBJECTIVES[req["role"]],
            },
            "effort": req["effort"],
            "answer": "",
        },
    )
    run = chatbot.stream_events(
        initial_state,
        config={
            "configurable": {
                "thread_id": _thread_id(user_id, req["chat_id"]),
            }
        },
        version="v3",
        # `custom` is not registered by default; the class is a scope-aware
        # factory, so it is cloned into every subgraph mini-mux. Tool progress
        # events therefore surface on each subgraph handle's `.custom`.
        transformers=[CustomTransformer],
    )

    def emit(event: ChatResponse) -> str:
        return json.dumps(event) + "\n"

    last_answer = None
    last_tool_results_json = None
    for name, item in run.interleave("messages", "values", "subgraphs", "custom"):
        if name == "messages":
            # Under langgraph v3 a root-scope LLM call surfaces here as a single
            # ``ChatModelStream``, not a list, so blind ``item[-1]`` indexing
            # would raise TypeError and tear the stream. Debug-only (no yield).
            if isinstance(item, (list, tuple)) and item:
                logger.debug("stream message: %s", getattr(item[-1], "text", None))
            else:
                logger.debug("stream message item: %s", type(item).__name__)
            # Forward the root-scope call's reasoning (iterable deltas, not a
            # list). Only a non-empty consolidated message is sent — an empty one
            # would append a bare "Thinking: " line to persisted progress.
            reasoning = getattr(item, "reasoning", None)
            if reasoning is not None:
                thought = ""
                for delta in reasoning:
                    thought += delta
                    yield emit(
                        {
                            "type": "thinking_delta",
                            "id": getattr(item, "message_id", "") or "",
                            "content": delta,
                        }
                    )
                if thought:
                    yield emit({"type": "subgraph_message", "content": thought})
        elif name == "values":
            # Change detection: a follow-up turn re-emits the previous turn's
            # payloads first, then the fresh one once find_evidence completes.
            # Sending on every change means the UI's final payload is current.
            tool_results = item.get("tool_results")
            if tool_results:
                payload = json.dumps(tool_results)
                if payload != last_tool_results_json:
                    yield emit({"type": "tool_results", "content": payload})
                    last_tool_results_json = payload
            answer = item.get("answer")
            if answer and answer != last_answer:
                last_answer = answer
            if "fail_reason" in item:
                logger.debug("pipeline fail_reason: %s", item["fail_reason"])
        elif name == "subgraphs":
            yield emit({"type": "subgraph_name", "content": str(item.graph_name)})
            for sub_name, sub_item in item.interleave("messages", "custom"):
                if sub_name == "messages":
                    thought = ""
                    for delta in sub_item.reasoning:
                        thought += delta
                        yield emit(
                            {
                                "type": "thinking_delta",
                                "id": sub_item.message_id or "",
                                "content": delta,
                            }
                        )
                    yield emit({"type": "subgraph_message", "content": thought})
                elif (
                    isinstance(sub_item, dict)
                    and sub_item.get("type") == "tool_progress"
                ):
                    yield emit(
                        {"type": "tool_progress", "content": json.dumps(sub_item)}
                    )
        elif name == "custom":
            # Root-scope custom producers: ``normalize``/``answer`` announce
            # their stage here via ``node_stage``, and any tool_progress is
            # forwarded with the same shape as the subgraph branch.
            if isinstance(item, dict) and item.get("type") == "tool_progress":
                yield emit({"type": "tool_progress", "content": json.dumps(item)})
            elif isinstance(item, dict) and item.get("type") == "node_stage":
                yield emit({"type": "stage", "content": str(item.get("name", ""))})

    yield emit({"type": "messages", "content": last_answer or ""})


@app.post("/chat/stream")
def chat_stream(
    req: ChatRequest, user: dict = Depends(require_user)
) -> StreamingResponse:
    user_id = user["id"]
    acquire_slot(user_id)
    metrics.inc("chat_streams_started")

    def guarded() -> Iterator[str]:
        try:
            yield from stream_events(req, user_id)
        finally:
            metrics.inc("chat_streams_completed")
            release_slot(user_id)

    return StreamingResponse(guarded(), media_type="application/x-ndjson")


@app.post("/chats/{chat_id}/investigate")
def start_investigation(
    chat_id: str, req: InvestigateRequest, user: dict = Depends(require_user)
) -> dict:
    """Start a background investigation and return its run id.
    Generation and persistence are owned by a worker thread, independent of this
    request. Watch via ``GET /chats/{chat_id}/run/{run_id}``."""
    user_id = user["id"]

    with _investigation_start_lock:
        # Idempotent per chat: a double send resumes the in-flight run instead of
        # persisting a second user message.
        existing = jobs.registry.active_run(user_id, chat_id)
        if existing is not None:
            return {"run_id": existing, "status": jobs.RUNNING}

        acquire_slot(user_id)
        try:
            chats.append_message(user_id, chat_id, "user", req["question"])
        except KeyError as e:
            release_slot(user_id)
            raise HTTPException(status_code=404, detail="Chat not found.") from e
        except Exception:
            release_slot(user_id)
            raise

        metrics.inc("chat_streams_started")

        request = cast(
            ChatRequest,
            {
                "chat_id": chat_id,
                "role": req["role"],
                "effort": req["effort"],
                "history": req["history"],
            },
        )

        def produce(_run: jobs.RunState) -> Iterator[str]:
            return stream_events(request, user_id)

        run = jobs.registry.start(
            user_id=user_id,
            chat_id=chat_id,
            produce=produce,
            on_finish=lambda finished: _finish_investigation(
                user_id, chat_id, finished
            ),
        )
        return {"run_id": run.run_id, "status": run.status}


def _finish_investigation(user_id: str, chat_id: str, run: jobs.RunState) -> None:
    """Persist a completed answer and release capacity (runs in the worker)."""
    metrics.inc("chat_streams_completed")
    try:
        if run.status == jobs.DONE:
            chats.append_message(
                user_id,
                chat_id,
                "assistant",
                run.answer,
                tool_results=run.tool_results,
                progress=run.progress,
            )
    except KeyError:
        # Chat deleted mid-run: nothing to persist, and that is fine.
        pass
    finally:
        release_slot(user_id)


@app.get("/chats/{chat_id}/run/{run_id}")
def get_run(
    chat_id: str,
    run_id: str,
    since: int = 0,
    wait: float = 0.0,
    user: dict = Depends(require_user),
) -> dict:
    """Poll a run: replay buffered events from ``since`` (optionally long-poll)."""
    run = jobs.registry.get(run_id)
    if run is None or run.chat_id != chat_id or run.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Run not found.")
    if wait > 0:
        jobs.registry.wait(run_id, since, timeout=wait)
    payload = jobs.registry.snapshot(run_id, since)
    if payload is None:  # evicted between get() and snapshot()
        raise HTTPException(status_code=404, detail="Run not found.")
    return payload


@app.get("/chats/{chat_id}/active-run")
def get_active_run(chat_id: str, user: dict = Depends(require_user)) -> dict:
    """The run currently investigating this chat, if any (for re-attach)."""
    return {"run_id": jobs.registry.active_run(user["id"], chat_id)}


@app.post("/chats/{chat_id}/run/{run_id}/cancel")
def cancel_run(chat_id: str, run_id: str, user: dict = Depends(require_user)) -> dict:
    run = jobs.registry.get(run_id)
    if run is None or run.chat_id != chat_id or run.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"cancelled": jobs.registry.cancel(run_id)}


@app.post("/chat/test")
async def chat_test(
    req: ChatRequest, user: dict = Depends(require_user)
) -> AsyncIterable[ChatResponse]:
    yield {"type": "subgraphs", "content": "normalize"}
    yield {"type": "messages", "content": "Hello, how "}
    yield {"type": "subgraphs", "content": "retrieval"}
    yield {"type": "subgraphs", "content": "search"}
    yield {"type": "messages", "content": "can I help you?"}


@app.get("/auto-complete", dependencies=[Depends(require_user)])
def get_auto_complete(query: str) -> dict:
    """Type-ahead suggestions merged across every local corpus.

    ``query`` must start with a selector scoping it to one field (``@``
    service, ``^`` template, ``$`` message text; a ``!`` right after the
    selector switches to case-sensitive ranking); a query with no leading
    selector returns no suggestions. Every local corpus loaded from a ``.log``
    file (``openstack``, ``bgl``) is searched and the suggestions are merged; a
    ``$`` suggestion carries the ``dataset`` its ``line_id`` belongs to. Only
    inverted indexes are read -- a keystroke must never trigger a scan of
    object storage.

    Always served in-process, whatever ``TOOLS_TRANSPORT`` is: auto-complete is
    a chat-box feature, not an evidence tool, so the MCP service does not offer
    it and a keystroke never pays for a network hop.
    """
    return {"suggestions": auto_complete(query)}


@app.post("/chat/reframe")
def chat_reframe(req: ReframeRequest, user: dict = Depends(require_user)) -> dict:
    """Re-explain an already-completed investigation for another role.
    Stateless one-shot: builds a synthetic prompt state and invokes the answer
    agent once (no graph/tools/thread). Sync so the model call runs off-loop."""
    state = cast(
        PipelineState,
        {
            "role": {
                "name": req["target_role"],
                "objective": ROLE_OBJECTIVES[req["target_role"]],
            },
            "effort": req["effort"],
            "normalized": req["question"],
            "evidence_report": {},
            "tool_results": req["tool_results"],
            "revision_feedback": "No revision is required.",
            "latest_answer": req["previous_answer"],
        },
    )
    structured = generate_answer(prompts.reframe(state))
    valid_ids = {record.get("id") for record in req["tool_results"] or []}
    cost = investigation_cost(req.get("tool_results") or [])
    answer = serialize_answer(structured, valid_ids, cost.get("note", ""))
    return {"answer": answer}


# --- Authentication -----------------------------------------------------------


class Credentials(BaseModel):
    username: str = Field(
        min_length=auth.USERNAME_MIN_LENGTH, max_length=auth.USERNAME_MAX_LENGTH
    )
    password: str = Field(
        min_length=auth.PASSWORD_MIN_LENGTH, max_length=auth.PASSWORD_MAX_LENGTH
    )


@app.post("/auth/register")
def register(req: Credentials) -> dict:
    if not auth.REGISTRATION_OPEN:
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    try:
        user = auth.create_user(req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    metrics.inc("users_registered")
    token, expires_at = auth.create_session(user["id"])
    metrics.inc("auth_sessions_created")
    return {
        "token": token,
        "expires_at": expires_at,
        "user": {"id": user["id"], "username": user["username"]},
    }


@app.post("/auth/login")
def login(req: Credentials) -> dict:
    user = auth.authenticate(req.username, req.password)
    if user is None:
        metrics.inc("auth_logins_failed")
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    metrics.inc("auth_logins_ok")
    token, expires_at = auth.create_session(user["id"])
    metrics.inc("auth_sessions_created")
    return {"token": token, "expires_at": expires_at, "user": user}


@app.post("/auth/logout")
def logout(token: str = Depends(auth.bearer_token)) -> dict:
    auth.revoke_session(token)
    metrics.inc("auth_sessions_revoked")
    return {"ok": True}


@app.get("/auth/me")
def me(user: dict = Depends(require_user)) -> dict:
    return user


# --- Chats --------------------------------------------------------------------


class CreateChat(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    role: RoleName
    title: str | None = Field(default=None, max_length=200)


class AppendMessage(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(max_length=200_000)
    tool_results: list | None = None
    progress: list | None = None
    reframed_for: str | None = None


@app.get("/chats")
def get_chats(user: dict = Depends(require_user)) -> dict:
    return {"chats": chats.list_chats(user["id"])}


@app.post("/chats")
def post_chat(req: CreateChat, user: dict = Depends(require_user)) -> dict:
    chat = chats.create_chat(user["id"], req.chat_id, req.role, req.title)
    metrics.inc("chats_created")
    return chat


@app.delete("/chats/{chat_id}")
def remove_chat(chat_id: str, user: dict = Depends(require_user)) -> dict:
    user_id = user["id"]
    # Cancel (and drop) any in-flight investigation before the chat disappears,
    # so its worker stops and its persistence attempt is discarded cleanly.
    active = jobs.registry.active_run(user_id, chat_id)
    if active is not None:
        jobs.registry.cancel(active)
        jobs.registry.discard(active)
    deleted = chats.delete_chat(user_id, chat_id)
    if deleted:
        metrics.inc("chats_deleted")
    return {"deleted": deleted}


@app.get("/chats/{chat_id}/messages")
def get_chat_messages(chat_id: str, user: dict = Depends(require_user)) -> dict:
    return {"messages": chats.get_messages(user["id"], chat_id)}


@app.post("/chats/{chat_id}/messages")
def post_chat_message(
    chat_id: str, req: AppendMessage, user: dict = Depends(require_user)
) -> dict:
    try:
        message = chats.append_message(
            user["id"],
            chat_id,
            req.role,
            req.content,
            tool_results=req.tool_results,
            progress=req.progress,
            reframed_for=req.reframed_for,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Chat not found.") from e
    metrics.inc("messages_appended")
    return message


# --- Operations ---------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up and serving."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: the app store and the log database are both reachable."""
    from backend.appdb.database import db
    from backend.db.connection import db_cursor

    checks: dict[str, str] = {}
    ok = True
    try:
        with db() as cur:
            cur.execute("SELECT 1")
        checks["app_store"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["app_store"] = f"error: {e}"
        ok = False
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
        checks["log_store"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["log_store"] = f"error: {e}"
        ok = False

    if os.getenv("TOOLS_TRANSPORT", "local").lower() == "mcp":
        try:
            from backend.pipeline.tools.mcp_bridge import get_mcp_bridge
            checks["mcp"] = "ok" if get_mcp_bridge().check() else "unavailable (local fallback enabled)"
            metrics.inc("mcp_readiness_checks_total")
        except Exception as e:  # noqa: BLE001
            checks["mcp"] = f"unavailable (local fallback enabled): {e}"
            metrics.inc("mcp_failures_total")
    else:
        checks["mcp"] = "not_configured"

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )


@app.get("/metrics")
def get_metrics() -> dict:
    """In-process counters plus live admission-control gauges."""
    with slots_lock:
        active_users = len(user_slots)
        inflight = sum(user_slots.values())
    snapshot = metrics.snapshot()
    snapshot["gauges"] = {
        "inflight_investigations": inflight,
        "active_users": active_users,
        "max_global_inflight": MAX_GLOBAL_INFLIGHT,
        "max_inflight_per_user": MAX_INFLIGHT_PER_USER,
    }
    return snapshot
