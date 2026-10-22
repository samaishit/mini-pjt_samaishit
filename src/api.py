# api.py - POST /query (과정 표준 API 규격)
"""실행: uvicorn src.api:api --port 8000
테스트(PowerShell):
  Invoke-RestMethod -Uri http://localhost:8000/query -Method Post -ContentType "application/json; charset=utf-8" `
    -Body '{"question": "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월에 여행 어디로 가면 좋을까?"}'
"""
import asyncio
import csv
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from src import config

INVALID_REQUEST_MESSAGE = f"question 은 1~{config.MAX_QUESTION_CHARS}자 문자열이어야 합니다. 예: {{\"question\": \"...\"}}"
UNAVAILABLE_MESSAGE = "죄송합니다. 일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."

WEB_DIST = config.PROJECT_ROOT / "web" / "dist"   # React 테스트 화면 빌드 결과 (cd web && npm run build)
EXAMPLES_CSV = config.PROJECT_ROOT / "evaluation" / "test_queries.csv"
NO_WEB_HTML = ("<!doctype html><meta charset='utf-8'><title>사주 라이프 플래너</title>"
               "<p>웹 화면이 아직 빌드되지 않았습니다. <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code> "
               "후 서버를 다시 시작하세요. API 는 <code>POST /query</code> 로 바로 쓸 수 있습니다.</p>")

app_state: dict = {}
log = logging.getLogger("saju.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if "app" not in app_state:   # 테스트는 미리 가짜 앱을 넣어 둘 수 있다
        from src.agent import build_app
        app_state["app"] = await build_app()
    yield
    app_state.pop("chat", None)
    app = app_state.pop("app", None)
    if app is not None:
        await app.close()


api = FastAPI(title="사주 라이프 플래너", lifespan=lifespan)


class Query(BaseModel):
    question: str = Field(min_length=1, max_length=config.MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blank question")
        return v


class ChatTurn(BaseModel):
    """웹 화면 '대화' 탭의 한 턴. session_id 가 없으면 새 대화를 연다."""
    message: str = Field(min_length=1, max_length=config.MAX_QUESTION_CHARS)
    session_id: str | None = Field(default=None, max_length=64)

    @field_validator("message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blank message")
        return v


def chat_service():
    """앱이 바뀌면(테스트의 가짜 앱 등) 대화 서비스도 새로 만든다."""
    from src.chat import ChatService
    svc = app_state.get("chat")
    if svc is None or svc.app is not app_state["app"]:
        svc = app_state["chat"] = ChatService(app_state["app"])
    return svc


@api.exception_handler(RequestValidationError)
async def invalid_request(_: Request, __: RequestValidationError):
    # 빈 질문, 1000자 초과, JSON 이 아닌 본문은 모두 같은 고정 문구로 422 를 돌려준다 (입력 원문은 되돌려 보내지 않음)
    return JSONResponse(status_code=422, content={"detail": INVALID_REQUEST_MESSAGE})


def _json(content: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=content,
                        headers={"Content-Type": "application/json; charset=utf-8"})


@api.post("/query")
async def query(q: Query):
    try:
        result = await app_state["app"].run_query(q.question)
    except Exception as e:   # Bedrock 장애 등: 원인 메시지(입력 일부가 섞일 수 있음)는 싣지 않고 종류만 남긴다
        etype = error_type(e)
        log.warning("query failed: %s", etype)
        return _json({"answer": UNAVAILABLE_MESSAGE, "contexts": [], "trace": [], "error_type": etype}, 503)
    return _json(result)


def error_type(e: BaseException) -> str:
    """예외 사슬에서 AWS 오류 코드(예: ThrottlingException)를 찾고, 없으면 예외 클래스 이름을 돌려준다."""
    seen, cur = set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = (getattr(cur, "response", None) or {}).get("Error", {}).get("Code")
        if code:
            return str(code)
        cur = cur.__cause__ or cur.__context__
    return type(e).__name__


@api.post("/chat")
async def chat(t: ChatTurn):
    """대화형 질의 (웹 화면 '대화' 탭). 세션마다 사주 요약·가린 질문·보여 준 장소를 기억한다.
    POST /query 는 과정 규약대로 상태 없이 그대로 둔다."""
    try:
        result = await chat_service().turn(t.session_id, t.message)
    except Exception as e:
        etype = error_type(e)
        log.warning("chat failed: %s", etype)
        return _json({"answer": UNAVAILABLE_MESSAGE, "contexts": [], "trace": [], "error_type": etype,
                      "session": {"session_id": t.session_id}}, 503)
    return _json(result)


@api.delete("/chat/{session_id}")
async def end_chat(session_id: str):
    """대화를 끝내고 기억을 지운다 ('새 대화' 버튼)."""
    return _json({"session_id": session_id, "deleted": chat_service().memory.drop(session_id)})


# ---------------------------------------------------------------------------
# 스트리밍 API (웹 화면용). 기존 /query·/chat 은 그대로 두고 따로 둔다.
# Server-Sent Events: stage(단계 알림) · step(trace 한 줄, 마스킹 후) · result(최종 응답) · error
# 답변 본문은 근거 검증이 끝난 뒤 result 로 한 번에 보낸다 (검증 전 문장을 내보내지 않음)
# ---------------------------------------------------------------------------
STREAM_KEEPALIVE_S = 10


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_run(run) -> StreamingResponse:
    """run(listener) 코루틴을 돌리며 진행 알림을 SSE 로 흘려보내고, 끝나면 result 를 보낸다."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def listener(kind: str, data: dict) -> None:   # LLM 콜백은 다른 스레드에서 올 수 있다
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    async def events():
        yield sse("stage", {"stage": "start", "label": "요청을 받았어요"})   # 계산을 띄우기 전에 바로 알린다
        task = asyncio.create_task(run(listener))
        try:
            while not (task.done() and queue.empty()):
                # 알림이 오거나 계산이 끝나는 쪽을 먼저 기다린다 (끝났는데 keepalive 시간만큼 기다리지 않게)
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, timeout=STREAM_KEEPALIVE_S,
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    kind, data = getter.result()
                    yield sse(kind, data)
                    continue
                getter.cancel()
                if not done:
                    yield ": keepalive\n\n"   # 오래 걸리는 단계에서도 연결을 유지한다
            try:
                result = task.result()
                yield sse("stage", {"stage": "done", "label": "답변이 준비됐어요"})
                yield sse("result", result)
            except Exception as e:
                etype = error_type(e)
                log.warning("stream failed: %s", etype)
                yield sse("error", {"message": UNAVAILABLE_MESSAGE, "error_type": etype})
        finally:
            if not task.done():   # 화면이 연결을 끊으면(취소) 진행 중인 계산도 멈춘다
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api.post("/query/stream")
async def query_stream(q: Query):
    """POST /query 와 같은 처리를 단계 알림과 함께 스트리밍한다 (웹 화면 '단일 질의' 탭)."""
    async def run(listener):
        result, _ = await app_state["app"].run_turn(q.question, listener=listener)
        return result
    return stream_run(run)


@api.post("/chat/stream")
async def chat_stream(t: ChatTurn):
    """POST /chat 과 같은 처리를 단계 알림과 함께 스트리밍한다 (웹 화면 '대화' 탭)."""
    async def run(listener):
        return await chat_service().turn(t.session_id, t.message, listener=listener)
    return stream_run(run)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/examples")
async def examples():
    """웹 화면의 예시 질문 목록 (평가셋 24건의 id·category·input, 가짜 출생 정보만 담김)."""
    with open(EXAMPLES_CSV, encoding="utf-8-sig") as f:
        rows = [{"id": int(r["id"]), "category": r["category"], "input": r["input"]} for r in csv.DictReader(f)]
    return _json({"reference_date": config.REFERENCE_DATE, "examples": rows})


# 웹 화면: 빌드 결과가 있으면 / 에서 서빙한다 (API 경로가 먼저 매칭되도록 맨 마지막에 붙인다)
if (WEB_DIST / "index.html").exists():
    api.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
else:
    @api.get("/", response_class=HTMLResponse)
    async def no_web():
        return NO_WEB_HTML
