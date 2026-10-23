# 사주 라이프 플래너 — LangGraph 노드 구성

`POST /query` 한 번이 거치는 LangGraph 그래프와 각 노드 내부를 코드 기준으로 그린 문서다.
그래프 정의는 [src/agent.py](../src/agent.py)의 `SajuPlannerApp._build_graph`, Supervisor는 `_build_supervisor`에 있다.

> LangGraph 의 `get_graph().draw_mermaid()` 는 람다로 건 조건부 분기를 그리지 못해(`guard_input → __end__` 만 보임) 아래 그림은 코드를 읽고 직접 그렸다.

## 1. 메인 그래프 (`StateGraph(QueryState)`)

```mermaid
flowchart TD
    START(["POST /query {question}"]) --> GI

    GI["guard_input<br/>제3자 규칙 → 출생 정보 파서 → 입력 가드(규칙 + LLM)"]
    GI -->|"차단·되묻기<br/>state.response 있음"| END_FIXED(["고정 문구 응답<br/>contexts = [ ]"])
    GI -->|"통과 (입력 가드 LLM 을 쓴 경우)<br/>saju·supervisor 를 가드와 동시에 미리 실행함"| COMP
    GI -->|"통과 (규칙만 쓴 경우)"| SAJU

    SAJU["saju<br/>LLM 없이 사주 MCP 직접 호출<br/>calculate_saju + get_luck_calendar → mcp:saju"]
    SAJU --> SUP

    SUP["supervisor<br/>의도 라우터 + 규칙 문서 선검색 + Supervisor 선택<br/>고른 에이전트 동시 실행 · 추천 도구 직접 보완"]
    SUP --> COMP

    COMP["compose<br/>Bedrock LLM with_structured_output(StructuredAnswer)<br/>claims[ section, text, doc_ids ]"]
    COMP --> GO

    GO["guard_output<br/>기간 교정 · doc_id 태그 제거 · 근거·금칙어·용신 오기 검사"]
    GO -->|"위반 있음 그리고 attempts < 2<br/>(부동산 부분 거절의 시세 문장은 재생성 없이 제거)"| COMP
    GO -->|"위반 없음 또는 2회째<br/>(2회째 위반 문장은 제거)"| FIN

    FIN["finalize<br/>코드 문장 조립 → 코드 문장 검사 → 한글(한자) 표기 → 렌더링"]
    FIN --> END_OK(["answer, contexts, trace"])
```

| 노드 | 함수 | LLM | 주요 trace step |
|---|---|---|---|
| `guard_input` | `guard_input` (+ 동시에 `_prepare` = `saju` → `run_supervisor`) | 입력 가드 판별 1회(치환 문장만) | `guard:input`, `parse:birth`, `llm:input_guard` |
| `saju` | `saju` → `tools.compute_chart` | 없음 | `tool:calculate_saju`, `tool:get_luck_calendar` (출력만 기록) |
| `supervisor` | `run_supervisor` | Supervisor 선택 1회 + 전문 에이전트(동시 실행) | `route`, `retrieve`, `agent:*`, `agent_tool:*`, `llm:agent` |
| `compose` | `compose` | 답변 작성 1회(+재생성 최대 1회) | `llm:compose` |
| `guard_output` | `guard_output` | 없음 | `guard:output` (`month_range_repair`, `doc_tag_strip`, `partial_refusal_drop`, `output_guard`, `drop`) |
| `finalize` | `finalize` | 없음 | `retrieve`(용어 풀이 근거), `guard:output`(`code_claim_check`) |

## 2. `guard_input` 내부 순서

```mermaid
flowchart LR
    Q["질문 원문"] --> TP{"제3자 규칙<br/>check_third_party<br/>(원문 대상)"}
    TP -->|해당| R1(["third_party 거절"])
    TP -->|아님| P["출생 정보 파서 parse<br/>원본 생년월일 → BIRTH 치환"]
    P --> RC{"규칙 가드 rule_check<br/>(치환 문장)"}
    RC -->|block| R2(["injection · off_topic · real_estate · explicit 거절"])
    RC -->|partial| CL
    RC -->|allow| LG["LLM 판별 llm_guard<br/>(치환 문장만 전송, 약 2초)"]
    RC -->|"allow 이고 되묻기 아님"| SPEC["_prepare 미리 시작<br/>saju → run_supervisor<br/>(asyncio task)"]
    LG -->|block| CANCEL["미리 시작한 작업 취소<br/>그 사이 trace·사주·contexts 폐기"] --> R2
    LG -->|ok| CL{"파서 되묻기?<br/>2자리 연도·달력 미표기·없는 날짜·성별 누락 등"}
    CL -->|예| R3(["되묻기 문구"])
    CL -->|"아니오 (미리 시작함)"| JOIN(["_prepare 결과(reports)를 받아<br/>compose 노드로"])
    CL -->|"아니오 (규칙만 씀)"| NEXT(["saju 노드로<br/>masked, birth, decision"])
    SPEC -.-> JOIN
```

- 원본 생년월일은 파서와 사주 MCP 호출 인자에만 남는다. 이후 LLM 프롬프트·trace·로그에는 `<BIRTH>`만 있다.
- 입력 가드 LLM 을 기다리는 동안 사주 계산과 전문 에이전트를 미리 돌린다. 차단되면 작업을 취소하고 그 사이의 trace·사주 요약·contexts 를 지우므로 거절 응답에는 아무것도 남지 않는다. 미리 돌린 에이전트 LLM 에는 차단될 질문도 들어가지만, 도구는 조회 전용이고 결과는 버린다.
- `partial` 이면 정상 부분은 답하고, 거절할 부분의 고정 문구는 `finalize`에서 덧붙인다.

## 3. `supervisor` 노드 내부

```mermaid
flowchart TD
    IN(["masked 질문 + 사주 요약(mcp:saju)"]) --> RT["의도 라우터 required_agents<br/>키워드 규칙 → 필수 에이전트 목록<br/>(1년 계획 → saju·travel·residence, 계획 평가 → plan …)"]
    RT --> IDX["대상 절기 월 번호 + 간지·기간 라벨<br/>날짜 구간을 절입일 기준으로 분할"]
    IDX --> PRE["규칙 문서 선검색 pre_retrieve_rules<br/>하이브리드 RAG(BM25 0.2 + 벡터 0.8, RRF c=60, top-3)<br/>+ 에이전트별 고정 문서 + 사주 요약 근거 문서"]
    PRE --> SV

    subgraph SV["create_supervisor (langgraph-supervisor)"]
        direction TB
        S["supervisor LLM 1회<br/>transfer_to_* 로 맡길 에이전트 선택<br/>post_model_hook = route_only<br/>(고른 이름을 기록하고 넘기기를 지워 바로 종료)"]
    end

    SV --> UNION["실행 목록 = 라우터 필수 에이전트 ∪ Supervisor 선택"]
    UNION --> PAR

    subgraph PAR["asyncio.gather 로 동시 실행 (각자 [이 에이전트의 담당] 범위를 받음)"]
        direction LR
        A1["saju_agent<br/>retrieve_rule_docs · element_direction_map"]
        A2["travel_agent<br/>recommend_travel · get_travel · retrieve_place_docs"]
        A3["residence_agent<br/>recommend_residence · get_region · retrieve_place_docs"]
        A4["plan_agent<br/>evaluate_plan · recommend_travel"]
    end

    PAR --> DET{"추천 도구가 불렸나?<br/>recommend_travel / recommend_residence"}
    DET -->|안 불림| CALL["도구 직접 호출<br/>(규칙대로 계산)"]
    DET -->|불림| OUT
    CALL --> OUT(["reports + state 에 도구 결과<br/>travel_picks · residence · plan_results"])
```

- langgraph-supervisor 는 넘기기를 하나씩 차례로 실행하고, 에이전트가 보고할 때마다 Supervisor LLM 을 다시 부른다(1년 계획 52초의 원인). 그래서 Supervisor 는 고르기만 하고 실행은 코드가 동시에 한다.
- 여행·주거·계획 에이전트의 보고는 400토큰·bullet 4개 이내다. 표·Top 3·등급 문장은 코드가 도구 결과로 만들기 때문이다. 사주 에이전트는 800토큰.

각 전문 에이전트는 `create_react_agent`로 만든 ReAct 루프다(LLM ↔ 도구). 도구는 아래 MCP 서버와 RAG를 부른다.

```mermaid
flowchart LR
    subgraph AG["전문 에이전트 도구 (src/tools.py)"]
        T1["recommend_travel<br/>get_travel"]
        T2["recommend_residence<br/>get_region"]
        T3["evaluate_plan"]
        T4["retrieve_rule_docs<br/>retrieve_place_docs"]
        T5["element_direction_map"]
    end
    subgraph MCP1["장소 DB MCP (FastMCP, Python)"]
        M1["search_travel · get_travel"]
        M2["search_regions · get_region"]
        M3["element_direction_map"]
    end
    subgraph MCP2["사주 MCP (mcp-saju 포크, Node)"]
        M4["calculate_saju"]
        M5["get_luck_calendar"]
    end
    subgraph RAG["하이브리드 RAG (src/retriever.py)"]
        R1["Chroma + Titan v2"]
        R2["BM25 + kiwi"]
    end
    T1 --> M1
    T2 --> M2
    T3 --> M1
    T3 --> M2
    T5 --> M3
    T4 --> R1
    T4 --> R2
    SAJUNODE["saju 노드"] --> M4
    SAJUNODE --> M5
```

## 4. `compose` → `guard_output` → `finalize`

```mermaid
flowchart TD
    C["compose<br/>LLM 이 쓰는 문장: 한눈에 보기 · 사주 해석 · 요약 문장"] --> G1["month_range_repair<br/>간지월 기간을 사주 계산값으로 교정"]
    G1 --> G2["doc_tag_strip<br/>본문의 doc_id 태그 제거"]
    G2 --> G3{"output_guard<br/>doc_id ∈ contexts · 간지→mcp:saju · 장소→장소 id<br/>운명론·부동산 금칙어 · 생년월일 재요청·추측 · 용신 오기"}
    G3 -->|"위반, 1회째"| C
    G3 -->|"위반, 2회째"| DROP["위반 문장 제거"]
    G3 -->|통과| F
    DROP --> F

    subgraph F["finalize (코드)"]
        direction TB
        F1["월별 여행 표 _travel_claims<br/>주거 Top 3·이사 시기 _residence_claims<br/>계획 평가 _plan_claims (DB 외 장소는 시기만 평가)"]
        F2["사주 요약 _summary_claims<br/>원국·일간·용신과 의미·세운·12개월 흐름"]
        F3["날짜 구간 분할 · 음력/윤달/시각 미상/계절 안내"]
        F4["섹션 정렬<br/>한눈에 보기 → 사주 요약 → 사주 해석 → 월별 운세 → 계획 평가 → 여행 → 주거 → 이사 → 안내"]
        F5["용어 풀이 _glossary_claims<br/>답변에 나온 용어만, 규칙 문서 doc_id 근거"]
        F6["근거 자료 _source_claim"]
        F7["code_claim_check<br/>코드 문장도 같은 검사, 위반이면 제거"]
        F8["한글(한자) 표기 annotate<br/>戊戌월 → 무술(戊戌)월"]
        F9["render_answer<br/>섹션 마크다운 + 부분 거절 문구 + 면책 문구"]
        F1 --> F2 --> F3 --> F4 --> F5 --> F6 --> F7 --> F8 --> F9
    end
    F --> OUT(["answer"])
```

- 장소 선정, 이사 좋은 달·피할 달, 계획 등급, 날짜 구간 분할은 **도구 결과로 코드가 문장을 만든다.** LLM은 결론 요약과 해석만 쓴다.
- 검증은 한자 원문으로 끝낸 뒤, 사용자에게 보여 주기 직전에만 `한글(한자)`로 바꾼다.

## 5. LLM 호출과 한도 초과 대체 (`src/llm.py`)

입력 가드, Supervisor, 전문 에이전트, 답변 작성은 모두 `make_llm()`이 만드는 `FallbackChatBedrockConverse`를 쓴다.

```mermaid
sequenceDiagram
    participant N as 노드(에이전트·작성·가드)
    participant L as FallbackChatBedrockConverse
    participant B1 as 기본 모델<br/>SAJU_LLM_MODEL 또는 Sonnet 4.5 (us.)
    participant B2 as global Sonnet 4.5
    participant B3 as us Sonnet 4.6
    N->>L: invoke / bind_tools / with_structured_output
    L->>B1: Converse (재시도 최대 3회, adaptive)
    alt 정상
        B1-->>L: 응답
    else ThrottlingException 등 한도 초과
        L->>B2: 같은 요청 (trace: llm:fallback)
        alt 정상
            B2-->>L: 응답
        else 또 한도 초과
            L->>B3: 같은 요청 (trace: llm:fallback)
            B3-->>L: 응답 또는 오류
        end
    end
    L-->>N: 결과 (모두 실패하면 503, error_type 기록)
```

- 한도·일시 불가 오류만 대체한다. 입력 오류(`ValidationException`)나 권한 오류는 바로 실패로 처리한다.
- 대체 순서는 `SAJU_LLM_FALLBACKS`로 바꾼다. 심사 모델(`JUDGE_MODEL`)은 라운드 비교를 위해 대체하지 않는다.

## 6. 대화 탭 (`POST /chat`, 단기 기억)

웹 화면의 '대화' 탭은 `POST /chat`을 쓴다. 채점 대상인 `POST /query`는 그대로 상태가 없다.
`/chat`은 세션(브라우저 대화)마다 짧은 기억을 서버 메모리에 두고, 같은 그래프를 `run_turn`으로 한 번 더 부른다([src/chat.py](../src/chat.py)).

```mermaid
flowchart TD
    U(["POST /chat {message, session_id?}"]) --> M["세션 찾기 / 새로 만들기<br/>ChatMemory (TTL 1시간, 최대 200개)"]
    M --> P["출생 정보 파서로 이번 말을 가림<br/>(원본은 기억하지 않음)"]
    P --> F{"이어 묻기인가?<br/>'그럼·다른…'으로 시작하거나<br/>짧고 의도 키워드가 없는 말"}
    F -->|예| C["앞 질문(가린 문장) + (이어서 묻는 말) 이번 말<br/>첫 질문 + 최근 이어 묻기 3개까지"]
    F -->|아니오| Q["이번 말 그대로"]
    C --> R
    Q --> R["SajuPlannerApp.run_turn<br/>preset_chart = 기억한 사주 요약 (새 생년월일이 오면 새로 계산)<br/>exclude_ids = '다른 곳' 요청이면 이미 보여 준 장소"]
    R --> G["메인 그래프 (1장과 같음)<br/>saju 노드: 사주 요약 재사용 (trace: memory:chart)<br/>이번 말의 달·계절·국내/해외를 우선"]
    G --> V["답변 필터 build_view (근거 검증이 끝난 최종 문장에만)<br/>① 이 대화에서 이미 보여 준 문장(섹션+문장 키)은 접음<br/>② 두 번째 턴부터 이번 말의 범위 섹션만 펼침 (직접 물은 섹션은 앞에서 봤어도 표시)<br/>'다시·전체·정리'면 필터 끔"]
    V --> S["기억 갱신: 사주 요약 · 가린 질문 · 보여 준 장소 id · 보여 준 문장 키 · 직전 답변 요약<br/>(거절·되묻기 턴은 맥락으로 쓰지 않음)"]
    S --> O(["answer(필터 후), answer_full, view(펼침/접힘), contexts, trace + session 요약"])
    X(["DELETE /chat/{session_id}<br/>'새 대화'"]) --> D["세션 기억 삭제"]
```

| 기억하는 것 | 기억하지 않는 것 |
|---|---|
| 계산된 사주 요약(간지·용신 등 파생값), `<BIRTH>`로 가린 질문, 답변 첫 줄, 보여 준 장소 id, 보여 준 문장 키(해시), 직전 답변 요약(다시 가린 문장, 600자) | 원본 생년월일·시각·성별, 원문 메시지 |

**답변 필터** (대화 탭에서만, `/query`는 그대로):
- 중복: 코드가 만든 문장(사주 요약·용어 풀이·여행 표 등)은 같은 입력이면 같은 문장이라 섹션+문장 키로 정확히 걸러진다. '한눈에 보기'는 늘 보여 준다.
- 범위: 이번 말에 의도 키워드가 있으면 그 섹션을, 없으면(예: "그럼 해외는?") 라우터가 고른 에이전트의 섹션을 펼친다. 범위 안 내용이 모두 앞에서 본 것이면 빈 답이 되지 않게 다시 보여 준다.
- 접은 문장도 응답 `view`에 남아 화면에서 펼쳐 볼 수 있다. 직전 답변 요약을 작성 LLM 에 넘겨 같은 해석을 되풀이하지 않게 한다.
- 예(실측): 두 번째 턴 "그럼 해외는?"의 답변이 1,636자 → 326자(앞에서 본 내용 12건 접음).

## 7. 스트리밍 API (`POST /query/stream`, `POST /chat/stream`)

웹 화면은 기다리는 동안 진행 상황을 보여 주려고 스트리밍 API 를 쓴다. 기존 `/query`·`/chat` 은 그대로다.
요청 상태(`RequestState`)에 진행 리스너를 달면, 노드가 시작할 때의 단계 알림(`emit`)과 trace 한 줄(`add_step`)이 그대로 흘러간다.
리스너가 없는 기존 API 에서는 아무것도 보내지 않으므로 응답(trace 포함)이 바뀌지 않는다.

```mermaid
sequenceDiagram
    participant W as 웹 화면
    participant A as FastAPI (stream_run)
    participant G as 메인 그래프 (run_turn)
    W->>A: POST /query/stream {question}
    A-->>W: stage: start (요청을 받았어요)
    A->>G: 별도 작업으로 실행 (listener 연결)
    loop 노드가 진행될 때마다
        G-->>A: stage (input · saju · route · compose · validate · finalize)
        A-->>W: event: stage
        G-->>A: trace 한 줄 (마스킹 후)
        A-->>W: event: step
    end
    Note over A,W: 10초 동안 알림이 없으면 keepalive 주석으로 연결 유지
    G-->>A: 최종 응답 (근거 검증 끝난 답변)
    A-->>W: stage: done, event: result
    W->>W: 진행 화면을 답변 화면으로 교체
```

- 답변 본문은 근거 검증이 끝난 뒤 `result` 로 한 번에 보낸다. LLM 출력을 글자 단위로 흘리면 검증 전 문장(나중에 지워질 문장, 근거 누락)이 먼저 보이기 때문이다.
- 화면을 닫거나 취소하면 서버의 계산 작업도 멈춘다. 오류면 `error` 이벤트(고정 문구 + 오류 종류)를 보내고 입력은 싣지 않는다.
- 입력 가드 LLM 은 동기 호출이라 별도 스레드에서 돌려, 그동안에도 진행 알림이 막히지 않게 했다.
- 화면의 로딩: 지금 하는 일, 진행률(단계 알림 기준, 알림이 없는 동안에도 다음 단계 직전까지 천천히 차오름), 단계 체크리스트, 최근 활동, 스켈레톤, 기다리는 동안 볼 명리 상식, 예상 소요 시간, 취소.

## 8. 요청 상태와 trace

- 요청마다 `tracing.start_request()`가 `RequestState`(trace, contexts, chart, travel_picks, residence, plan_results, route, date_spans)를 새로 만든다. 상태는 요청 사이에 공유하지 않는다(멀티턴 없음).
- trace 의 input/output 은 마스킹한 뒤 300자로 자른다. 사주 MCP 호출은 출력만 남긴다.
