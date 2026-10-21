# 미니 PJT: 사주 라이프 플래너 (Saju Life Planner)

## 무엇을 푸나
사주 운세·여행·주거 정보가 흩어져 있어, 연말연시에 **내 운의 흐름에 맞춘 1년 여행·이사 계획**을 근거 있게 세울 수 없는 20~40대 직장인의 문제를 푼다. 출생 정보가 담긴 질문 하나로 12개 절기 월의 세운·월운을 계산하고, 월별 여행지·주거 지역 Top 3·이사 시기·내 계획의 적합도를 **근거(doc_id)와 함께** 답한다. 상세 기획은 [SERVICE.md](SERVICE.md).

## 활용한 패턴 (Day 1~7)
| # | 패턴 | 활용 |
|---|---|---|
| 1 | LCEL + Pydantic 구조화 출력 (Day 1) | 최종 답변을 `StructuredAnswer{claims[{section, text, doc_ids}]}`로 생성 → 섹션별 렌더링 ([src/schemas.py](src/schemas.py)) |
| 2 | ReAct (Day 3) | 전문 에이전트 4종을 `create_react_agent`로 구성, 도구 자율 선택 |
| 3 | RAG: 하이브리드 검색 + 쿼리 확장 (Day 2) | kiwi 형태소 BM25 + Chroma(Titan v2), EnsembleRetriever 0.5/0.5 RRF c=60, 용신 오행 키워드 결정론 확장 ([src/retriever.py](src/retriever.py)) |
| 4 | 도구 다중 (Day 4) | 사주 계산·여행/주거 추천·계획 평가·문서 검색 도구를 한 질의에 조합 |
| 5 | MCP 서버 연동 (Day 4) | ① mcp-saju **포크**(Node, 기준일 `now`·`get_luck_calendar` 추가) ② 장소 DB FastMCP 서버. 세션을 열어 두고 재사용 |
| 6 | 가드레일 (Day 5) | 제3자 규칙 → 출생 정보 파서 → 규칙 1차 → LLM 2차 판별, 고정 거절 문구, 부분 거절, 출력 금칙어 검사 ([src/guards.py](src/guards.py)) |
| 8 | 미들웨어: PII 마스킹·출력 검증·재시도 (Day 5) | 생년월일 `<BIRTH>` 치환(LLM 미전송), trace 2중 마스킹, 출력 doc_id 검증 → 1회 재생성 → 위반 문장 제거, 절기 월 기간 자동 교정 |
| 9 | Multi-Agent Supervisor (Day 6) | `langgraph-supervisor`로 saju/travel/residence/plan 에이전트 위임 + 의도 라우터가 필수 에이전트 누락을 보완 |
| 11 | Observability · Trace (Day 7) | 요청별 TraceRecorder(콜백 + 노드 직접 기록) → API 응답 `trace`, 감사 로그 ([src/tracing.py](src/tracing.py)) |
| 12 | 평가: RAGAS + LLM-as-Judge (Day 7) | 인-아웃 세트 24건 규칙+심사 판정, RAGAS 4지표 ([evaluation/](evaluation)) |

## 아키텍처
```
POST /query {question}
   │
   ▼
guard_input ─ ① 제3자 규칙(원문) ② 출생 정보 파서(<BIRTH> 치환, 되묻기) ③ 규칙 가드 ④ LLM 판별(치환 문장)
   │   └─ 차단/되묻기 → 고정 문구 응답 (contexts=[])
   ▼
saju ─ LLM 없이 사주 MCP 직접 호출: calculate_saju + get_luck_calendar → 사주 요약(mcp:saju)
   ▼
supervisor ─ 의도 라우터가 필수 에이전트 지정
   ├─ saju_agent      : retrieve_rule_docs, element_direction_map
   ├─ travel_agent    : recommend_travel(→ MCP search_travel), get_travel, retrieve_place_docs
   ├─ residence_agent : recommend_residence(→ MCP search_regions), get_region, retrieve_place_docs
   └─ plan_agent      : evaluate_plan(→ MCP search_travel/search_regions)
   ▼
compose ─ Bedrock LLM with_structured_output(StructuredAnswer)
   ▼
guard_output ─ 절기 월 기간 교정 · doc_id ∈ contexts · 필수 근거(간지→mcp:saju, 장소→장소 id) · 금칙어
   │   └─ 위반 → compose 1회 재생성 → 그래도 위반이면 해당 문장 제거
   ▼
finalize ─ 섹션 렌더링 + 부분 거절 문구 + 면책 문구 → {answer, contexts, trace}
```
- 장소 선정·이사 길흉월·계획 등급은 **규칙(코드)으로 계산**하고 LLM은 설명만 한다 (기준은 [data/rules](data/rules)에 문서화).
- 기준일은 `2026-10-21`로 고정(재현성). 환경 변수 `SAJU_REFERENCE_DATE`로만 변경. 12개월 범위 = 戊戌월(2026-10-08) ~ 己酉월(2027-10-07).

## 실행 방법
```bash
# 0) 사전 준비: Python 3.14, Node 24, uv, AWS Bedrock 자격 증명(.env: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION)
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 1) 사주 MCP 포크 빌드 (최초 1회)
cd mcp_servers/saju && npm install && npm run build && npm test && cd ../..

# 2) RAG 인덱스 생성 (규칙 43 + 장소 70 = 113건)
python -m src.retriever --build

# 3) API 실행
uvicorn src.api:api --port 8000
#   PowerShell: Invoke-RestMethod -Uri http://localhost:8000/query -Method Post -ContentType "application/json; charset=utf-8" -Body '{"question": "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월에 여행 어디로 가면 좋을까?"}'
#   또는 한 번만: python -m src.agent "질문"

# 4) 단위 테스트 (Bedrock 불필요)
python -m unittest discover -s tests -t .

# 5) 평가: 인-아웃 세트 실행·판정·리포트 → evaluation/roundN_report.md
python -m evaluation.run_eval --round 1
#    RAGAS 는 평가 전용 venv(Python 3.12, ragas 가 3.14 휠 미지원)
uv venv --python 3.12 .venv-eval && uv pip install --python .venv-eval/Scripts/python.exe -r requirements-eval.txt
.venv-eval\Scripts\python evaluation/ragas_eval.py --round 1
```
- Bedrock 일일 토큰 한도에 걸리면 `SAJU_LLM_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0`로 모델을 바꿔 실행할 수 있다 (기본 Claude Sonnet 4.5).

## RAGAS 평가 결과
(round 2 결과 반영 예정 — round 1 값은 [evaluation/round1_report.md](evaluation/round1_report.md))
- context_recall: -
- context_precision: -
- faithfulness: -
- answer_relevancy: -

## 인-아웃 세트 통과율 (자체 평가)
- 1차 (Day 9): - / 24 통과
- 2차 (Day 10 개선 후): - / 24 통과
- 개선폭: -

## 트라이앤에러 회고
- **사주 MCP 선정**: OpenFate bazi-mcp는 세운·월운 도구가 없고, molpass/mcp-saju는 기준일을 받지 않아 2027년 월운을 못 구했다 → 포크해 `now`와 절기 경계를 스캔하는 `get_luck_calendar`를 추가. 정오 판정으로는 입동(11-07 오후)이 하루 밀려 23:59 KST 판정으로 바꿨다.
- **RAGAS 환경**: Python 3.14에서 ragas 의존성(scikit-network) 빌드 실패, ragas 0.4.3이 langchain-community 0.4에서 삭제된 모듈을 import → 평가 전용 Python 3.12 venv + LangChain 0.3 계열로 분리.
- **개인정보**: 사주 MCP의 JSON 출력에 입력 생년월일이 그대로 들어 있어 trace에 원본 연도가 남는 것을 발견 → 사주 도구는 입력·출력을 trace에 남기지 않고 요약(mcp:saju)만 노출.
- **부동산 데이터**: 네이버 부동산은 robots.txt 수집 금지·약관·가드레일(시세 금지) 충돌 → 공개 자료 기반으로 시·구 생활 환경만 직접 작성.
- **LLM의 월 매핑 오류**: "내년 5월"을 甲辰월로 잘못 말함 → 사주 요약에 '양력 N월(15일 기준)' 열을 넣고, 출력 단계에서 `간지월(기간)` 표기를 코드로 교정.
- **Supervisor 누락**: 연간 요청에서 Supervisor가 여행 에이전트를 건너뜀 → 키워드 의도 라우터로 필수 에이전트를 지정하고 누락 시 직접 호출.
- (round 1 → round 2 개선 내역 추가 예정)

## 핵심 코드 위치
- `src/agent.py:163` — 메인 에이전트 그래프 (`SajuPlannerApp`, 그래프 구성 `:184`, 입력 가드 `:207`, Supervisor `:232`, 출력 검증 `:296`)
- `src/agent.py:40` — 질문 의도 라우터 (`required_agents`)
- `src/tools.py` — 도메인 도구 (MCP 허브 `:46`, 사주 계산 `:121`, 여행 추천 `:303`, 주거 추천 `:347`, 계획 평가 `:383`)
- `src/retriever.py` — RAG 파이프라인 (인덱스 생성 `:108`, 쿼리 확장 `:148`, 하이브리드 검색 `:159`)
- `src/birth_parser.py:212` — 결정론 출생 정보 파서
- `src/guards.py` — 가드레일 (고정 문구 `:19`, 제3자 `:66`, 입력 가드 `:140`, 출력 검사 `:165`)
- `src/tracing.py` — 요청별 trace·contexts (`RequestState :42`, `TraceCallback :95`)
- `src/api.py` — `POST /query`
- `mcp_servers/saju/src/index.ts` — 사주 MCP 포크, `mcp_servers/places/places_server.py` — 장소 DB MCP
- `evaluation/run_eval.py`, `evaluation/judge.py`, `evaluation/ragas_eval.py` — 평가

## 데이터 출처와 라이선스
- 사주 계산: [molpass/mcp-saju](https://github.com/molpass/mcp-saju) 포크 + ssaju 엔진 (MIT, [LICENSE](mcp_servers/saju/LICENSE))
- 장소 DB·명리 규칙 문서: 공개 자료(지자체·관광 안내, 위키백과 등)를 참고해 직접 작성한 **교육용 데이터**. 시세·매물 정보 없음.
- 사주 해석은 참고용 엔터테인먼트 정보이며 의료·법률·투자 판단의 근거가 아니다.
