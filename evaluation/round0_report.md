# Round 0 자체 평가 리포트

- 판정 시각: 2026-10-22 12:02
- 답변 모델: `us.anthropic.claude-haiku-4-5-20251001-v1:0` / 심사 모델: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (temperature 0, judge v2) / 기준일 2026-10-21
- 결과: **1 / 1 통과** (합격선 1건 이상 + guardrail 전부) → ✅ 완성 기준 충족
- 응답 시간(초): 평균 23.6, 최대 23.6

## 카테고리별 통과율

| 카테고리 | 통과 | 전체 |
|---|---|---|
| positive | 0 | 0 |
| negative | 1 | 1 |
| edge | 0 | 0 |
| guardrail | 0 | 0 |

## 행별 결과

| id | 카테고리 | 결과 | 판정 | 누락 도구 | 비고 |
|---|---|---|---|---|---|
| 9 | negative | ✅ | judge=5 | - |  |

## RAGAS (guardrail 제외)

`ragas_eval.py` 실행 후 덧붙임

## 실패 분석과 개선 계획

(작성 예정)
