# CLAUDE.md

사주 라이프 플래너: 출생 정보가 들어 있는 자연어 질문 하나를 받아, 기준일부터 12개 절기 월의 사주(세운·월운)를 계산하고 월별 여행지, 주거지 Top 3와 이사 시기, 계획 적합도를 근거와 함께 답하는 Agentic RAG API다. 기획은 [SERVICE.md](SERVICE.md), 평가셋은 [evaluation/test_queries.csv](evaluation/test_queries.csv)를 따른다.

삼성SDS AX 교육 미니 프로젝트다(Day 8~10, 제출 2026-10-23 15:00, public GitHub). 과정 산출물 규약(폴더 구조, `POST /query` 규격, SERVICE.md·README 양식, CSV 스키마)은 **바꾸면 안 되는 제약**이다. 코드 패턴과 패키지 버전은 참고 레포 `C:\Users\SDS\Desktop\sds-ax-practice`(day2 RAG, day4 MCP, day5 가드레일, day6 Supervisor, day7 평가·트레이스)를 따른다.

## 기술스택

| 영역 | 선택 | 버전 (참고 레포 venv 기준) |
|---|---|---|
| 언어 | Python | 3.14 (설치 실패 시 3.13 → 3.12로 내리고 README에 기록) |
| LLM | AWS Bedrock Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us-east-1` |
| LLM 클라이언트 | `langchain-aws` `ChatBedrockConverse` | 1.8.1 |
| 프레임워크 | `langchain` / `langchain-core` / `langchain-classic` | 1.4.4 / 1.6.9 / 1.0.8 |
| 그래프 | `langgraph`, `langgraph-prebuilt` (`create_react_agent`) | 1.2.14 / 1.1.0 |
| 멀티 에이전트 | `langgraph-supervisor` (`create_supervisor`) | 0.0.31 |
| MCP 클라이언트 | `langchain-mcp-adapters` (`MultiServerMCPClient`, stdio) | 0.3.2 |
| MCP 서버 (장소 DB) | `fastmcp` / `mcp` | 2.13.0 / 1.30.0 |
| MCP 서버 (사주) | molpass/mcp-saju **포크** (Node/TS, ssaju 엔진, MIT) | Node 24, ssaju 0.2.0 |
| RAG | `langchain-chroma` + `chromadb`, `rank-bm25` + `kiwipiepy`, Bedrock `amazon.titan-embed-text-v2:0` | 1.1.0 + 1.5.9, 0.2.2 + 0.24.0 |
| API | FastAPI + uvicorn | 설치 시점 버전으로 고정 |
| 평가 | `ragas` (Bedrock LLM·Titan 임베딩 래핑) + LLM 심사 | Day 8 설치 시험 후 고정. 안 되면 3.12 평가 전용 venv → 자체 구현 지표 순으로 대체 |
| 구조화 출력 | `pydantic` | 2.14.1 |

## 디렉터리 구조 (과정 규약 §4-1 기준)

```
mini-pjt_samaishit/
├── SERVICE.md / README.md / CLAUDE.md
├── requirements.txt / .env.example / Dockerfile(선택) / run.sh(선택)
├── src/
│   ├── api.py            # FastAPI: POST /query
│   ├── agent.py          # 메인 그래프: 가드 → 파서 → 사주 계산 → Supervisor → 출력 검증
│   ├── tools.py          # 도메인 도구 (MCP 클라이언트 + retrieve_docs 래핑)
│   ├── retriever.py      # 하이브리드 RAG (LLM 모듈 import 금지)
│   ├── birth_parser.py   # 결정론 출생 정보 파서 (<BIRTH> 치환, 되묻기)
│   ├── guards.py         # 입력·출력 가드, REFUSAL_MESSAGES 고정 문구
│   ├── tracing.py        # 요청별 TraceRecorder (마스킹, 300자 절단)
│   ├── schemas.py        # 답변 Pydantic 모델 (claims[{text, doc_ids}])
│   ├── llm.py            # Bedrock LLM 생성 + 한도 초과 시 대체 모델(MODEL_FALLBACKS) 재호출
│   ├── hanja.py          # 간지·오행 한자 → '한글(한자)' 표기 (사용자 출력 직전)
│   ├── glossary.py       # 답변에 나온 명리 용어의 일상어 풀이 (규칙 문서 doc_id 근거)
│   └── config.py         # MODEL, MODEL_FALLBACKS, REFERENCE_DATE 등 상수
├── mcp_servers/
│   ├── saju/             # mcp-saju 포크 (now 파라미터, get_luck_calendar)
│   └── places/           # FastMCP 장소 DB 서버
├── data/
│   ├── rules/*.md        # 명리 해석 규칙 문서 (교육용 직접 작성)
│   ├── places/regions.json, travel.json   # 가짜 장소 DB
│   └── index/            # Chroma 영속 저장소 (build_index로 생성)
├── evaluation/
│   ├── test_queries.csv  # 인-아웃 세트 24건 (과정 7개 컬럼)
│   ├── references.json   # RAGAS 정답 요지 (guardrail 제외 16건)
│   ├── run_eval.py / judge.py / ragas_eval.py
│   └── round1_report.md / round2_report.md
├── web/                  # React(Vite+TS) 테스트 화면: POST /query 클라이언트, 빌드 결과 web/dist 를 FastAPI 가 / 에서 서빙
└── tests/                # 파서·판정 함수 단위 테스트 (Bedrock 불필요)
```

## 핵심 설계 규칙

- **API 계약**: `POST /query {"question"}` → `{answer, contexts:[{doc_id, text, source?, doc_type?}], trace:[{step, input, output, type?, name?, latency_ms?}]}`. 과정 키는 그대로 두고 선택 필드만 더한다. 상태 없음(멀티턴 없음).
- **trace step 이름**: `guard:input`, `parse:birth`, `retrieve`, `tool:<이름>`, `agent:<이름>`, `llm:<노드>`, `guard:output`. input/output은 마스킹 후 300자로 자른다. 사주 MCP 호출은 output만 기록한다.
- **처리 순서**: 제3자 규칙 검사 → 출생 정보 파서 → 입력 가드 LLM 판별(치환된 문장만) → 사주 계산(LLM 없이 MCP 직접 호출) → Supervisor(질문 의도에 따라 saju/travel/residence/plan 에이전트 선택) → 출력 검증.
- **개인정보**: 생년월일 원문은 파서와 사주 MCP 호출 인자에만 존재한다. LLM 프롬프트, 로그, trace, 평가 결과 파일에는 `<BIRTH>`만 남긴다. 계산된 간지·십성·용신은 LLM에 보내도 된다.
- **파서 되묻기**: 2자리 연도, 양/음력 미표기, 존재하지 않는 날짜·윤달, 성별 누락, 오전/오후 없는 시각, 생일 후보 여러 개, 미래 날짜 → LLM 호출 없이 되묻기. 시각 미상 → 시주 제외, 음력에 '윤' 없음 → 평달.
- **근거**: 간지와 용신은 ssaju 결과만 쓰고 LLM이 추론하지 않는다. 장소는 MCP 점수 도구가 고른 후보에서만 고른다. 답변은 `claims[{text, doc_ids}]` 구조화 출력이고, 출력 미들웨어가 doc_id가 `contexts`에 있는지와 반드시 인용해야 하는 주장(간지·용신·월 범위 → `mcp:saju`, 해석 → `rule:*`, 장소 → 장소 id 또는 `mcp:search_*`)을 검사한다. 위반 시 1회 재생성, 그래도 위반이면 해당 문장을 뺀다.
- **RAG**: Chroma 컬렉션 하나 + 메모리 BM25(kiwi, doc_type으로 미리 거름), EnsembleRetriever 0.2/0.8(BM25/벡터, `evaluation/retrieval_sweep.py`로 0.2·0.5·0.8 비교 후 결정), RRF c=60 명시, top-k 3. 결정론 쿼리 확장(용신 오행, 월지, 방위 키워드). 리랭킹 없음. doc_id: `rule:<topic>:<slug>`, 장소 id, `mcp:<도구>`.
- **기준일**: `config.REFERENCE_DATE = 2026-10-21`. 환경 변수 `SAJU_REFERENCE_DATE`로만 바꾼다(미설정이어도 시스템 날짜가 아니라 고정값). 이 값을 MCP `now`, 프롬프트의 '기준일', trace에 그대로 전달한다. 12개월 범위 = 2026-10-08 ~ 2027-10-07.
- **절기 월**: 월 경계는 절입일이며 23:59 KST로 판정한다. 계획의 양력 월은 그 달 15일이 속한 절기 월로 매핑한다.
- **가드레일**: 고정 문구 `REFUSAL_MESSAGES`(injection, off_topic, third_party, real_estate, explicit)만 쓴다. 계획 문장 속 인젝션은 전체 거절. 정상 요청에 부동산·도메인 이탈이 섞이면 해당 부분만 거절하고 나머지는 답한다. 운명론 질문은 거절하지 않고 주의점으로 바꿔 말하며 면책 문구를 붙인다.
- **평가**: 24건, 합격선은 전체 20건 이상 그리고 guardrail 8/8. 일반 행 = LLM 심사 4점 이상(Sonnet 4.5, temperature 0, 1~5점, 1회) + forbidden 0건(부정문 허용, 고정 문구 제외) + expected_tools ⊆ trace 도구 이름. guardrail 행 = 규칙 판정, 실제 `/query`를 끝까지 호출. 오류·타임아웃(180초)은 실패로 집계하고 Bedrock 일시 오류는 2회 재시도. RAGAS 4지표는 guardrail 제외 16건 평균으로만 보고한다.
- **코드 스타일**: 참고 레포처럼 한국어 docstring과 주석을 쓰고, `ChatBedrockConverse` 응답은 `get_text()`로 꺼낸다. MCP 도구 docstring에는 언제 쓰고 언제 쓰지 않는지 적는다.
- **비밀값**: `.env`는 커밋하거나 읽어서 출력하지 않는다. `.env.example`만 제공한다.
