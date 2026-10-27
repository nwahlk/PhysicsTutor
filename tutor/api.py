import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, StringConstraints, model_validator

from tutor.catalog import load_catalog
from tutor.graph import MODES, build_graph
from tutor.models import build_model_bundle
from tutor.practice import CalculationAnswer
from tutor.store import PgStore

logger = logging.getLogger("tutor")

Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
CATALOG = load_catalog()
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        conninfo=os.environ["DATABASE_URL"],
        min_size=1,
        max_size=int(os.environ.get("POOL_MAX_SIZE", "5")),
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    try:
        # 表结构由 migrate.py 负责，这里不调用 setup()
        models = build_model_bundle()
        store = PgStore(pool)
        app.state.graph = build_graph(
            AsyncPostgresSaver(pool), catalog=CATALOG, models=models, store=store
        )
        app.state.store = store
        yield
    finally:
        await pool.close()


app = FastAPI(title="高中物理辅导 Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    student_id: Ident
    thread_id: Ident
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    kp_id: str | None = None
    mode: str | None = None


class AnswerRequest(BaseModel):
    student_id: Ident
    thread_id: Ident
    answer: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | CalculationAnswer

    @model_validator(mode="after")
    def validate_size(self):
        if len(self.model_dump_json().encode("utf-8")) > 16 * 1024:
            raise ValueError("请求体不能超过 16 KiB")
        return self


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def run_stream(graph, graph_input, config: dict) -> AsyncIterator[str]:
    try:
        seen_interrupts: set[str] = set()
        emitted_grades: set[tuple[str, int]] = set()
        async for streamed in graph.astream(
            graph_input, config, stream_mode="updates", subgraphs=True
        ):
            chunk = streamed[1] if isinstance(streamed, tuple) else streamed
            for node, update in chunk.items():
                if node == "__interrupt__":
                    for item in update:
                        if item.id not in seen_interrupts:
                            seen_interrupts.add(item.id)
                            previous = item.value.get("previous_grade", {})
                            if previous:
                                key = (previous["question_id"], previous["attempt_no"])
                                if key not in emitted_grades:
                                    emitted_grades.add(key)
                                    yield sse({"type": "grade", "result": previous})
                                if item.value.get("hint"):
                                    yield sse({"type": "hint", "content": item.value["hint"]})
                            yield sse({"type": "interrupt", "id": item.id, "payload": item.value})
                elif node == "record" and isinstance(update, dict) and update.get("grade_result"):
                    result = update["grade_result"]
                    key = (result["question_id"], result["attempt_no"])
                    if key not in emitted_grades:
                        emitted_grades.add(key)
                        yield sse({"type": "grade", "result": result})
                elif node == "practice" and isinstance(update, dict) and update.get("grade_result"):
                    result = update["grade_result"]
                    key = (result["question_id"], result["attempt_no"])
                    if key not in emitted_grades:
                        emitted_grades.add(key)
                        yield sse({"type": "grade", "result": result})
                elif node == "hint" and isinstance(update, dict) and update.get("hint"):
                    yield sse({"type": "hint", "content": update["hint"]})
                elif node == "respond" and isinstance(update, dict):
                    for message in update.get("messages", []):
                        yield sse({"type": "token", "content": message.content})
        yield sse({"type": "done"})
    except Exception:
        logger.exception("graph run failed")
        yield sse({"type": "error", "detail": "服务内部错误，请稍后再试"})


def stream_response(graph, graph_input, config: dict) -> StreamingResponse:
    return StreamingResponse(
        run_stream(graph, graph_input, config),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def pending_interrupts(graph, thread_id: str) -> list:
    snapshot = await graph.aget_state(thread_config(thread_id))
    return list(snapshot.interrupts)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    try:
        await request.app.state.store.ping()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/catalog")
async def catalog():
    return {"subject": CATALOG.subject, "textbook": CATALOG.textbook, "chapters": CATALOG.tree}


@app.post("/chat")
async def chat(body: ChatRequest, request: Request):
    if body.kp_id is not None and CATALOG.get(body.kp_id) is None:
        raise HTTPException(422, f"未知知识点: {body.kp_id}")
    if body.mode is not None and body.mode not in MODES:
        raise HTTPException(422, f"mode 只能是 {', '.join(MODES)}")
    store = request.app.state.store
    graph = request.app.state.graph
    if not await store.claim_thread(body.student_id, body.thread_id):
        raise HTTPException(403, "该会话不属于当前学生")
    if await pending_interrupts(graph, body.thread_id):
        raise HTTPException(409, "当前有题目等待作答，请先通过 /answer 提交")
    graph_input = {
        "messages": [HumanMessage(content=body.message)],
        "student_id": body.student_id,
        "thread_id": body.thread_id,
        "turn_id": uuid.uuid4().hex,
        "turn_kp_id": body.kp_id,
        "turn_mode": body.mode,
    }
    return stream_response(graph, graph_input, thread_config(body.thread_id))


@app.post("/answer")
async def answer(body: AnswerRequest, request: Request):
    store = request.app.state.store
    graph = request.app.state.graph
    owner = await store.thread_owner(body.thread_id)
    if owner is not None and owner != body.student_id:
        raise HTTPException(403, "该会话不属于当前学生")
    pending = await pending_interrupts(graph, body.thread_id) if owner is not None else []
    if not pending:
        raise HTTPException(409, "当前没有等待作答的题目")
    payload = pending[0].value
    if payload.get("type") == "kp_choice":
        if not isinstance(body.answer, str):
            raise HTTPException(422, "知识点选择必须提交候选 ID")
        allowed = {item["id"] for item in payload["candidates"]}
        if body.answer not in allowed:
            raise HTTPException(422, "请选择候选知识点")
        resume_value = body.answer
    elif payload.get("type") == "question":
        if not isinstance(body.answer, CalculationAnswer):
            raise HTTPException(422, "练习题必须提交结构化答案")
        if body.answer.question_id != payload["question"]["id"]:
            raise HTTPException(422, "答案 question_id 与当前题目不一致")
        resume_value = body.answer.model_dump(mode="json")
    else:
        raise HTTPException(422, "未知的待处理交互")
    return stream_response(graph, Command(resume=resume_value), thread_config(body.thread_id))


@app.get("/history")
async def history(
    request: Request,
    student_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")],
    thread_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")],
):
    owner = await request.app.state.store.thread_owner(thread_id)
    if owner is None:
        return {"thread_id": thread_id, "messages": [], "focus_kp_id": None, "mode": None, "pending": []}
    if owner != student_id:
        raise HTTPException(403, "该会话不属于当前学生")
    snapshot = await request.app.state.graph.aget_state(thread_config(thread_id))
    values = snapshot.values
    return {
        "thread_id": thread_id,
        "messages": [{"role": m.type, "content": m.content} for m in values.get("messages", [])],
        "focus_kp_id": values.get("focus_kp_id"),
        "mode": values.get("mode"),
        "pending": [{"id": item.id, "payload": item.value} for item in snapshot.interrupts],
    }
