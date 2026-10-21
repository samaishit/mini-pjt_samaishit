# api.py - POST /query (과정 표준 API 규격)
"""실행: uvicorn src.api:api --port 8000
테스트(PowerShell):
  Invoke-RestMethod -Uri http://localhost:8000/query -Method Post -ContentType "application/json; charset=utf-8" `
    -Body '{"question": "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월에 여행 어디로 가면 좋을까?"}'
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.agent import SajuPlannerApp, build_app

app_state: dict[str, SajuPlannerApp] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    app_state["app"] = await build_app()
    yield
    await app_state["app"].close()


api = FastAPI(title="사주 라이프 플래너", lifespan=lifespan)


class Query(BaseModel):
    question: str


@api.post("/query")
async def query(q: Query):
    result = await app_state["app"].run_query(q.question)
    return JSONResponse(content=result, headers={"Content-Type": "application/json; charset=utf-8"})


@api.get("/health")
async def health():
    return {"status": "ok"}
