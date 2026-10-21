# api.py - POST /query (과정 표준 API 규격)
"""실행: uvicorn src.api:api --port 8000
테스트(PowerShell):
  Invoke-RestMethod -Uri http://localhost:8000/query -Method Post -ContentType "application/json; charset=utf-8" `
    -Body '{"question": "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월에 여행 어디로 가면 좋을까?"}'
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from src import config

INVALID_REQUEST_MESSAGE = f"question 은 1~{config.MAX_QUESTION_CHARS}자 문자열이어야 합니다. 예: {{\"question\": \"...\"}}"
UNAVAILABLE_MESSAGE = "죄송합니다. 일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."

app_state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if "app" not in app_state:   # 테스트는 미리 가짜 앱을 넣어 둘 수 있다
        from src.agent import build_app
        app_state["app"] = await build_app()
    yield
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
    except Exception:   # Bedrock 장애 등: 원인 메시지(입력 일부가 섞일 수 있음)는 응답에 싣지 않는다
        return _json({"answer": UNAVAILABLE_MESSAGE, "contexts": [], "trace": []}, 503)
    return _json(result)


@api.get("/health")
async def health():
    return {"status": "ok"}
