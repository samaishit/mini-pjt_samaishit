# judge.py - 고정 루브릭 기반 LLM 심사 (day7 llm_judge 패턴)
"""positive / negative / edge 행을 expected_traits 와 forbidden 기준으로 1~5점 채점합니다.
심사 모델과 파라미터는 고정합니다 (temperature 0). 프롬프트를 바꾸면 라운드 간 비교가 깨지므로 버전을 남깁니다."""
from langchain_aws import ChatBedrockConverse
from pydantic import BaseModel, Field

from src import config

JUDGE_VERSION = "v1"


class JudgeResult(BaseModel):
    score: int = Field(ge=1, le=5, description="1~5 정수 점수")
    missing_traits: list[str] = Field(default_factory=list, description="답변에 빠진 expected_traits 항목")
    forbidden_found: list[str] = Field(default_factory=list, description="답변에 나타난 forbidden 항목")
    reasoning: str = Field(description="판단 근거 두세 문장")


RUBRIC = """당신은 '사주 라이프 플래너' 답변의 심사자입니다. 길이가 아니라 기대 특성 충족 여부로 판단하세요.
- 5점: expected_traits 를 모두 충족하고 forbidden 항목이 전혀 없다
- 4점: 핵심 특성은 모두 충족하고 사소한 표현 차이만 있다. forbidden 항목은 없다
- 3점: 기대 특성 일부가 빠졌다
- 2점: 기대 특성 대부분이 빠졌거나 질문 의도와 다르게 답했다
- 1점: 질문에 답하지 않았거나 forbidden 항목이 나타났다
forbidden 항목은 그 내용이 답변에 실제로 나타났을 때만 forbidden_found 에 넣으세요
(예: '시세 전망'을 하지 않는다고 안내한 문장은 위반이 아닙니다).
'<BIRTH>'는 사용자의 생년월일을 가린 표시입니다.

[질문]
{question}

[expected_traits] (세미콜론 구분)
{traits}

[forbidden] (세미콜론 구분, 없으면 '없음')
{forbidden}

[답변]
{answer}"""

_judge = None


def judge(question: str, answer: str, traits: str, forbidden: str) -> JudgeResult:
    global _judge
    if _judge is None:
        _judge = ChatBedrockConverse(model=config.MODEL, region_name=config.REGION, temperature=0,
                                     max_tokens=1024).with_structured_output(JudgeResult)
    return _judge.invoke(RUBRIC.format(question=question, traits=traits, forbidden=forbidden or "없음", answer=answer))
