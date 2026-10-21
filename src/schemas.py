# schemas.py - 답변 구조화 출력 (LCEL + Pydantic)
"""최종 답변은 '근거가 달린 주장(claim)'의 목록으로 만듭니다.
사용자에게 보이는 answer 는 claims 를 섹션별로 이어 붙여 만들고,
출력 미들웨어가 각 claim 의 doc_ids 가 contexts 에 있는지 검사합니다."""
from pydantic import BaseModel, Field


class Claim(BaseModel):
    section: str = Field(description="답변 섹션 제목 (예: '사주 요약', '월별 여행 추천', '주거 추천', '이사 시기', '계획 평가')")
    text: str = Field(description="사용자에게 보여 줄 한두 문장. 존댓말.")
    doc_ids: list[str] = Field(default_factory=list,
                               description="이 문장의 근거 doc_id 목록 (contexts 에 있는 것만). 일반 안내 문장은 빈 목록")


class StructuredAnswer(BaseModel):
    claims: list[Claim] = Field(description="답변을 구성하는 문장들. 순서대로 화면에 표시된다.")


def render_answer(answer: StructuredAnswer, extra: list[str] | None = None, disclaimer: str | None = None) -> str:
    """claims 를 섹션 순서대로 마크다운 텍스트로 만든다."""
    lines: list[str] = []
    current = None
    for c in answer.claims:
        if c.section != current:
            if lines:
                lines.append("")
            lines.append(f"### {c.section}")
            current = c.section
        lines.append(f"- {c.text}")
    for msg in extra or []:
        lines += ["", msg]
    if disclaimer:
        lines += ["", disclaimer]
    return "\n".join(lines).strip()
