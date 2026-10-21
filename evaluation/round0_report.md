# Round 0 자체 평가 리포트

- 판정 시각: 2026-10-21 17:36
- 답변 모델: `us.anthropic.claude-haiku-4-5-20251001-v1:0` / 심사 모델: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (temperature 0, judge v2) / 기준일 2026-10-21
- 결과: **1 / 4 통과** (합격선 4건 이상 + guardrail 전부) → ❌ 미충족
- 응답 시간(초): 평균 43.6, 최대 106.5

## 카테고리별 통과율

| 카테고리 | 통과 | 전체 |
|---|---|---|
| positive | 1 | 3 |
| negative | 0 | 0 |
| edge | 0 | 1 |
| guardrail | 0 | 0 |

## 행별 결과

| id | 카테고리 | 결과 | 판정 | 누락 도구 | 비고 |
|---|---|---|---|---|---|
| 1 | positive | ✅ | judge=5 | - |  |
| 6 | positive | ❌ | judge=3 | - | 생활에서의 활용 예(방위·환경); 해석 근거 문서 인용 |
| 7 | positive | ❌ | judge=3 | - | 巳午未월의 명확한 명리 기준 설명 |
| 13 | edge | ❌ | judge=2 | - | 윤달을 반영해 양력 1993-05-01로 계산; 12개 절기 월별 여행지 |

## RAGAS (guardrail 제외)

`ragas_eval.py` 실행 후 덧붙임

## 실패 분석과 개선 계획

(작성 예정)
