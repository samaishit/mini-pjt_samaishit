# config.py - 프로젝트 공통 상수
"""모델 id, 경로, 기준일처럼 여러 모듈이 함께 쓰는 값을 한 곳에서 관리합니다."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Bedrock
REGION = "us-east-1"
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
# Bedrock 일일 토큰 한도에 걸리면 SAJU_LLM_MODEL 로 임시 교체한다 (예: us.anthropic.claude-haiku-4-5-20251001-v1:0)
MODEL = os.getenv("SAJU_LLM_MODEL", DEFAULT_MODEL)
# 심사(LLM-as-Judge) 모델은 답변 모델과 분리해 고정한다. SAJU_LLM_MODEL 의 영향을 받지 않는다
JUDGE_MODEL = os.getenv("JUDGE_MODEL", DEFAULT_MODEL)
BEDROCK_READ_TIMEOUT_S = 60
MCP_CALL_TIMEOUT_S = 30
MAX_QUESTION_CHARS = 1000
EMBED_MODEL = "amazon.titan-embed-text-v2:0"

# 데이터 경로
DATA_DIR = PROJECT_ROOT / "data"
RULES_DIR = DATA_DIR / "rules"
PLACES_DIR = DATA_DIR / "places"
INDEX_DIR = DATA_DIR / "index" / "chroma"
COLLECTION = "saju_planner"

# 기준일: 미설정이어도 시스템 날짜가 아니라 고정값을 씁니다 (재현성)
DEFAULT_REFERENCE_DATE = "2026-10-21"
REFERENCE_DATE = os.getenv("SAJU_REFERENCE_DATE", DEFAULT_REFERENCE_DATE)
date.fromisoformat(REFERENCE_DATE)  # 형식이 틀리면 시작할 때 바로 실패
