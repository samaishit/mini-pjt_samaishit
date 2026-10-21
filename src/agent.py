# agent.py - 메인 에이전트 그래프 (LangGraph StateGraph + Supervisor)
"""POST /query 한 번을 처리하는 그래프입니다.

guard_input → saju → supervisor(saju/travel/residence/plan 전문 에이전트) → compose → guard_output → finalize
- guard_input: 제3자 규칙(원문) → 출생 정보 파서 → 규칙 가드 → LLM 판별(<BIRTH> 치환 문장)
- saju: LLM 없이 사주 MCP 를 직접 호출해 사주 요약을 만든다 (원본 생년월일은 여기까지만)
- compose: 전문 에이전트 보고를 근거가 달린 claims 로 구조화 (LCEL + Pydantic)
- guard_output: doc_id 존재·필수 근거·금칙어 검사, 위반이면 1회 재생성 후 위반 문장 제거
"""
import asyncio
import re
from typing import Any, TypedDict

from botocore.exceptions import ReadTimeoutError

from botocore.config import Config as BotoConfig
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from src import config, guards, tools
from src.birth_parser import parse
from src.schemas import Claim, StructuredAnswer, render_answer, strip_doc_tags
from src.tracing import RequestState, TraceCallback, start_request

GANZHI_CHARS = set("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# '己亥월(2026-11-07 ~ 2026-12-06)' 형태의 기간 표기 (교정 대상)
MONTH_RANGE_RE = re.compile(r"([甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥])월\s*\(\s*\d{4}-\d{2}-\d{2}\s*[~∼-]\s*(?:\d{4}-)?\d{2}-\d{2}\s*\)")

# 질문 의도 → 반드시 호출할 전문 에이전트 (결정론 키워드 규칙, Supervisor 에 힌트로 주고 누락 시 직접 보완)
PLAN_EVAL_RE = re.compile(r"(\d{1,2}\s*월|내년|올해|이번)[^.?!]{0,30}(여행|이사)[^.?!]{0,20}(괜찮|어때|어떨|해도 될|가도 될)"
                          r"|계획[^.?!]{0,20}(괜찮|어때|어떨)")
FULL_YEAR_RE = re.compile(r"(앞으로|향후|내년|올해)?\s*(1년|일\s*년|한\s*해|연간)\s*(동안\s*)?(계획|운세|흐름)")
# 여행 에이전트는 '추천'을 원할 때만 (예: '여행 가면 어느 달 운?'은 사주 해석)
TRAVEL_RE = re.compile(r"여행지|(여행|휴가)[^.?!]{0,15}(추천|어디|좋을까|계획\s*(세워|짜))|(추천|어디)[^.?!]{0,10}(여행|휴가)")
RESIDENCE_RE = re.compile(r"이사|주거|살기|동네|지역")
SAJU_RE = re.compile(r"운세|용신|오행|십성|기운|좋은\s*달|조심|주의할|어느\s*달|운으로|운이야|운인지|의미|사주")


# 에이전트별로 검색 결과와 함께 늘 넘길 규칙 문서 (추천·평가 근거를 답변에 인용할 수 있게)
RULE_DOCS_BY_AGENT = {
    "travel_agent": ["rule:direction:oheng", "rule:travel:timing"],
    "plan_agent": ["rule:travel:plan-rating", "rule:residence:move-timing"],
}
# 계획 전용 질문에서 코드가 만든 '계획 평가'로 대체하는 작성자 섹션
PLAN_REPLACED_SECTIONS = {"계획 평가", "여행 추천", "이사 시기", "주거 추천"}


def required_agents(question: str) -> list[str]:
    """질문에서 반드시 거쳐야 할 전문 에이전트를 고른다."""
    if PLAN_EVAL_RE.search(question) and "어디" not in question:
        need = ["plan_agent"]
        if SAJU_RE.search(question):
            need.insert(0, "saju_agent")
        return need
    need = []
    if SAJU_RE.search(question):
        need.append("saju_agent")
    if TRAVEL_RE.search(question):
        need.append("travel_agent")
    if RESIDENCE_RE.search(question):
        need.append("residence_agent")
    if FULL_YEAR_RE.search(question) and not (TRAVEL_RE.search(question) or RESIDENCE_RE.search(question)):
        need = ["saju_agent", "travel_agent", "residence_agent"]
    return need or ["saju_agent"]


def llm(max_tokens: int = 4096) -> ChatBedrockConverse:
    # 호출마다 읽기 타임아웃을 걸어 Bedrock 이 응답하지 않아도 요청이 무한정 걸리지 않게 한다
    boto_config = BotoConfig(read_timeout=config.BEDROCK_READ_TIMEOUT_S, connect_timeout=10,
                             retries={"max_attempts": 2, "mode": "standard"})
    return ChatBedrockConverse(model=config.MODEL, region_name=config.REGION, temperature=0, max_tokens=max_tokens,
                               config=boto_config)


ERROR_MESSAGE = "죄송합니다. 일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
BIRTH_REQUEST_RE = re.compile(r"(생년월일|출생\s*정보|음력\s*생년월일|양력\s*생년월일)[^.]{0,20}(알려|필요|입력|주시면|주세요)")
# <BIRTH> 로 가려진 출생 연·월을 LLM 이 짐작해 쓴 표현 (예: '양력 12월 생', '1988년생')
BIRTH_GUESS_RE = re.compile(r"(\d{1,2}\s*월|\d{2,4}\s*년)\s*(출생|생)(이|인|의|입니다)?(?![가-힣])")
# 오행 한자의 한글 읽기 (조사 고르기용)
ELEMENT_READING = {"木": "목", "火": "화", "土": "토", "金": "금", "水": "수"}


def josa(word: str, with_final: str, without_final: str) -> str:
    """앞말의 받침 유무로 조사를 고른다 (한자 오행은 한글 읽기로 판단). 예: josa('삿포로', '은', '는') → '는'"""
    last = ELEMENT_READING.get(word[-1], word[-1]) if word else ""
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 else without_final
    return with_final


def get_text(message) -> str:
    """ChatBedrockConverse 는 content 를 블록 리스트로 주기도 하므로 텍스트만 모은다."""
    content = message.content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------
COMMON_RULES = """[공통 규칙]
- 간지·용신·절기 월 기간은 사주 요약(mcp:saju)에 적힌 값만 인용한다. 직접 계산하거나 추측하지 않는다.
- 장소는 도구 결과에 나온 것만 말한다. DB 에 없는 장소의 일정·가격·명소를 지어내지 않는다.
- 죽음·질병·사고·이혼을 단정하지 않는다. 부정적 흐름은 '주의할 점'으로 바꿔 말한다.
- 부동산은 지역의 기운과 이사 시기까지만 말한다. 시세·집값 전망·매수·매도·청약·대출·투자는 말하지 않는다.
- 사주 요약이 없으면(출생 정보 없음) 개인 맞춤 추천을 하지 말고 생년월일이 필요하다고 보고한다.
- 보고는 한국어 짧은 bullet 로 하고, 각 사실 뒤에 근거 doc_id 를 [doc_id] 형태로 붙인다
  (예: [mcp:saju], [rule:yongsin:overview], [T-OS-SAPPORO], [tool:evaluate_plan])."""

SAJU_PROMPT = f"""너는 사주 해석 전문가(saju_agent)다. 용신·오행·십성의 의미, 좋은 달과 조심할 달, 특정 기간의 운,
절기 경계 해석을 담당한다. 해석 근거가 필요하면 retrieve_rule_docs 를 1~2회 호출해 규칙 문서를 인용한다.
좋은 달은 '용신 일치'가 있고 '충'이 없는 달, 조심할 달은 일지 '충'이 있는 달이다.
날짜 구간이 절기 경계에 걸치면 경계 전후를 나누어 설명한다.
{COMMON_RULES}"""

TRAVEL_PROMPT = f"""너는 여행 추천 전문가(travel_agent)다. 월별 여행지는 반드시 recommend_travel 로 고르고, 필요한 달을 모두 담아 한 번만 호출한다.
- 메시지의 [대상 절기 월 번호]를 month_indices 로 그대로 쓴다. 없으면 사주 요약의 '양력 N월' 열에서 질문이 가리키는 달의 번호를 고른다.
- 계절은 명리 기준이다: 봄=寅卯辰월, 여름=巳午未월, 가을=申酉戌월, 겨울=亥子丑월.
- 국내/해외 요청에 맞춰 scope 를 고른다(언급 없으면 both).
각 달의 pick 을 추천하고 이유는 점수 근거(용신 오행·방위·추천 월)로 설명한다. 동행자가 있어도 사용자 본인 사주 기준이라고 밝힌다.
{COMMON_RULES}"""

RESIDENCE_PROMPT = f"""너는 주거 추천 전문가(residence_agent)다. 주거 지역과 이사 시기는 반드시 recommend_residence 로 구한다.
Top 3 지역과 그 이유(오행·방위·환경), 이사하기 좋은 달과 피할 달(일지 충)을 보고한다.
투자 판단이 아니라는 점을 한 줄로 밝힌다.
{COMMON_RULES}"""

PLAN_PROMPT = f"""너는 계획 평가 전문가(plan_agent)다. 사용자가 말한 계획(언제 어디로 여행, 언제 이사 등)마다
evaluate_plan 을 한 번씩 호출한다. '내년'은 기준일 다음 해다. 결과의 rating(좋음/보통/주의), 근거,
대안(alternatives, better_move_months)을 그대로 보고한다. status 가 out_of_window 면 범위 밖이라 평가하지 않는다고 보고한다.
in_db 가 false 면 'DB 외 장소'라 상세 평가를 할 수 없다고 밝히고 대안을 제시한다.
{COMMON_RULES}"""

SUPERVISOR_PROMPT = """너는 사주 라이프 플래너의 작업 분배자(Supervisor)다. 직접 답하지 말고 질문 의도에 맞는 전문 에이전트만 호출한다.
- 사주 해석·용신 의미·좋은/조심할 달·특정 기간의 운·건강이나 사고를 묻는 운세 → saju_agent
- 여행지 추천 → travel_agent
- 주거 지역·이사 시기 추천 → residence_agent
- 사용자가 직접 세운 계획(특정 달의 여행·이사)의 적합도 → plan_agent
- '1년 계획', '앞으로 1년' 같은 전체 요청 → saju_agent, travel_agent, residence_agent 를 차례로 모두 호출 (계획이 있으면 plan_agent 도)
- 범위 밖 질문(조회 범위 밖 날짜, DB 에 없는 장소)도 해당 에이전트에 넘겨 사실대로 보고받는다.
필요한 에이전트가 모두 보고하면 '완료'라고만 답하고 끝낸다."""

COMPOSE_PROMPT = """당신은 사주 라이프 플래너의 최종 답변 작성자입니다. 아래 전문 에이전트 보고와 근거 목록만 사용해
사용자 질문에 답하는 claims 를 만드세요.

[작성 규칙]
1. 질문이 묻는 범위만 답한다. 좁은 질문(예: 특정 달 여행지)에 12개월 전체를 나열하지 않는다.
2. 각 claim 은 존댓말 한두 문장이고, doc_ids 에는 아래 [근거 목록]에 있는 doc_id 만 넣는다.
3. 간지·용신·절기 월 기간(YYYY-MM-DD)을 말하는 claim 은 doc_ids 에 mcp:saju 를 넣는다.
4. 장소를 말하는 claim 은 그 장소의 id(예: T-OS-SAPPORO, R-SEOUL-MAPO)를 doc_ids 에 넣는다. 계획 평가 결과는 tool:evaluate_plan 도 넣는다.
5. 해석·의미 설명은 관련 rule:* 문서를 doc_ids 에 넣는다.
6. 절기 월은 '己亥월(2026-11-07 ~ 2026-12-06)'처럼 날짜 범위와 함께 쓴다.
7. 죽음·질병·사고·이혼을 단정하지 말고 주의점으로 바꿔 말하며, 건강 문제는 전문가 상담을 권한다.
8. 시세·집값·매수·매도·청약·대출·투자 표현을 쓰지 않는다.
9. 출생 시각 미상이면 시주를 제외했다는 안내를 넣는다. DB 외 장소, 조회 범위 밖이면 그 사실을 분명히 말한다.
10. 동행자가 언급되면 본인 사주 기준이라고 밝힌다. 사주 요약이 없으면 생년월일을 요청하는 claim 하나만 쓴다.
11. 섹션 이름은 '사주 요약', '월별 운세', '여행 추천', '주거 추천', '이사 시기', '계획 평가', '안내' 중에서 고른다.
12. '당신'이라고 부르지 말고 주어를 생략하거나 '~님'처럼 정중하게 쓴다.
13. 계획 평가 claim 은 '좋음/보통/주의' 등급 이름과 해당 절기 월 기간을 반드시 함께 쓴다.
14. 여러 달을 추천하는 요청이면 보고에 나온 달마다 국내·해외 추천을 빠짐없이 옮긴다.
15. 용신 일치 오행은 사주 요약 표의 '용신 일치' 값 그대로 쓴다.
16. 근거 목록에 mcp:saju 가 있으면 사주 요약이 있는 것이다. 이때 생년월일을 다시 묻지 않는다.
    생년월일은 <BIRTH> 로 가려져 있으므로 출생 연·월('12월생', '1988년생' 등)을 짐작해 쓰지 않는다.
17. 용신·오행·십성의 의미를 묻는 질문에는 rule 문서를 근거로 개념, 사용자 사주에서의 의미, 생활 활용(방위·환경·계절) 예시를 각각 한 문장 이상 쓴다.
18. doc_id 는 doc_ids 필드에만 넣고 text 에는 [doc_id] 같은 태그를 쓰지 않는다.
    대신 해석 근거는 "명리 해설 「용신이란」에 따르면"처럼 문서 제목으로 자연스럽게 밝힌다.
{travel_note}
{feedback}
[사용자 질문]
{question}

[근거 목록] (doc_id: 내용 일부)
{contexts}

[전문 에이전트 보고]
{reports}"""


# ---------------------------------------------------------------------------
# 그래프 상태와 노드
# ---------------------------------------------------------------------------
class QueryState(TypedDict, total=False):
    question: str
    masked: str
    birth: Any
    decision: Any
    response: str            # 가드·파서가 끝낸 경우의 고정 답변
    reports: str
    answer: StructuredAnswer
    violations: list[str]
    attempts: int
    final: str


class SajuPlannerApp:
    def __init__(self, hub: tools.MCPHub, use_llm_guard: bool = True):
        self.hub = hub
        self.use_llm_guard = use_llm_guard
        self.supervisor = self._build_supervisor()
        self.composer = llm(8192).with_structured_output(StructuredAnswer)
        self.graph = self._build_graph()

    # ---- 구성 ----
    def _build_supervisor(self):
        model = llm()
        self.agents = {
            "saju_agent": create_react_agent(model, tools.SAJU_TOOLS, prompt=SAJU_PROMPT, name="saju_agent"),
            "travel_agent": create_react_agent(model, tools.TRAVEL_TOOLS, prompt=TRAVEL_PROMPT, name="travel_agent"),
            "residence_agent": create_react_agent(model, tools.RESIDENCE_TOOLS, prompt=RESIDENCE_PROMPT,
                                                  name="residence_agent"),
            "plan_agent": create_react_agent(model, tools.PLAN_TOOLS, prompt=PLAN_PROMPT, name="plan_agent"),
        }
        return create_supervisor(list(self.agents.values()), model=model, prompt=SUPERVISOR_PROMPT,
                                 output_mode="last_message").compile()

    def _build_graph(self):
        g = StateGraph(QueryState)
        g.add_node("guard_input", self.guard_input)
        g.add_node("saju", self.saju)
        g.add_node("supervisor", self.run_supervisor)
        g.add_node("compose", self.compose)
        g.add_node("guard_output", self.guard_output)
        g.add_node("finalize", self.finalize)
        g.set_entry_point("guard_input")
        g.add_conditional_edges("guard_input", lambda s: END if s.get("response") else "saju")
        g.add_edge("saju", "supervisor")
        g.add_edge("supervisor", "compose")
        g.add_edge("compose", "guard_output")
        g.add_conditional_edges("guard_output",
                                lambda s: "compose" if s.get("violations") and s.get("attempts", 0) < 2 else "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    # ---- 노드 ----
    def _state(self) -> RequestState:
        from src.tracing import current
        return current()

    async def guard_input(self, s: QueryState) -> QueryState:
        st = self._state()
        if guards.check_third_party(s["question"]):
            guards.log_block("third_party", "제3자 규칙")
            st.add_step("guard:input", "(원문 생략)", "block: third_party", type="guard", name="third_party")
            return {"response": guards.REFUSAL_MESSAGES["third_party"]}
        parsed = parse(s["question"], config.REFERENCE_DATE)
        st.add_step("parse:birth", parsed.masked_text,
                    f"birth={'ok' if parsed.birth else 'none'}, clarification={parsed.clarification}",
                    type="parse", name="birth_parser")
        decision = guards.input_guard(parsed.masked_text, parsed.birth is not None, use_llm=self.use_llm_guard)
        st.add_step("guard:input", parsed.masked_text, f"{decision.action}: {decision.category or decision.partial_categories}",
                    type="guard", name="input_guard")
        if decision.action == "block":
            guards.log_block(decision.category, decision.reason)
            return {"response": decision.message}
        if parsed.clarification:
            return {"response": parsed.clarification_message}
        return {"masked": parsed.masked_text, "birth": parsed.birth, "decision": decision}

    async def saju(self, s: QueryState) -> QueryState:
        if s.get("birth"):
            await tools.compute_chart(s["birth"], config.REFERENCE_DATE)
        return {}

    async def run_supervisor(self, s: QueryState) -> QueryState:
        st = self._state()
        chart_block = tools.chart_text(st.chart) if st.chart else "(출생 정보 없음: 사주 요약이 없습니다)"
        need = required_agents(s["masked"])
        st.route = need
        indices = tools.target_month_indices(s["masked"])
        content = (f"[기준일] {config.REFERENCE_DATE}\n[사주 요약 · doc_id=mcp:saju]\n{chart_block}\n\n"
                   f"[사용자 질문]\n{s['masked']}\n\n[필수 호출 에이전트] {', '.join(need)}")
        if indices:
            # 번호만 주면 양력 월로 오해하므로 간지와 기간을 함께 적는다
            months = {m["index"]: m for m in st.chart["months"]}
            labels = ", ".join(f"{i}번={months[i]['month_ganzhi']}월({months[i]['start_date']} ~ {months[i]['end_date']})"
                               for i in indices)
            content += f"\n[대상 절기 월 번호] {indices} → {labels} (절기 월 번호이며 양력 월이 아님)"
        extra = [d for name, ids in RULE_DOCS_BY_AGENT.items() if name in need for d in ids]
        if "saju_agent" in need or extra:
            # 해석 근거가 필요한 질문은 규칙 문서를 먼저 검색해 에이전트에게 함께 넘긴다 (RAG)
            rules = tools.pre_retrieve_rules(s["masked"], extra_ids=extra)
            content += "\n\n[관련 해석 규칙]\n" + "\n".join(f"- [{r['doc_id']}] {r['text']}" for r in rules)
        st.add_step("route", s["masked"], {"agents": need, "month_indices": indices}, type="route", name="intent_router")
        callbacks = {"callbacks": [TraceCallback(st)], "recursion_limit": 40}
        result = await self.supervisor.ainvoke({"messages": [HumanMessage(content=content)]}, callbacks)
        reports, done = [], set()
        for m in result["messages"]:
            if isinstance(m, AIMessage) and getattr(m, "name", None) not in (None, "supervisor") and get_text(m).strip():
                reports.append(f"## {m.name}\n{get_text(m)}")
                done.add(m.name)
        # Supervisor 가 필수 에이전트를 건너뛰었으면 직접 호출해 보완한다
        for name in need:
            if name in done:
                continue
            st.add_step(f"agent:{name}", "fallback", "Supervisor 누락 보완", type="agent", name=name)
            sub = await self.agents[name].ainvoke({"messages": [HumanMessage(content=content)]}, callbacks)
            last = next((m for m in reversed(sub["messages"]) if isinstance(m, AIMessage) and get_text(m).strip()), None)
            if last:
                reports.append(f"## {name}\n{get_text(last)}")
        # 여행 추천이 필요한데 월별 추천 도구가 불리지 않았으면 규칙대로 직접 계산한다
        if "travel_agent" in need and st.chart and not st.travel_picks:
            scope = "domestic" if "국내" in s["masked"] and "해외" not in s["masked"] else \
                "overseas" if "해외" in s["masked"] and "국내" not in s["masked"] else "both"
            await tools.recommend_travel.ainvoke({"month_indices": indices or list(range(1, 13)), "scope": scope})
            st.add_step("agent:travel_agent", "fallback", "recommend_travel 직접 호출", type="agent", name="travel_agent")
        return {"reports": "\n\n".join(reports) or "(보고 없음)"}

    def _travel_claims(self) -> list[Claim]:
        """recommend_travel 결과로 월별 여행 추천 문장을 만든다 (LLM 없이, 근거 doc_id 포함)."""
        st = self._state()
        if not st.chart or not st.travel_picks:
            return []
        ys = set(st.chart["yongsin_elements"])
        months = {m["index"]: m for m in st.chart["months"]}
        basis = ["mcp:saju"] + [d for d in ("rule:direction:oheng",) if d in st.contexts]
        claims = [Claim(section="여행 추천",
                        text=f"용신이 {'·'.join(st.chart['yongsin_elements'])}이므로, 그 달에 가기 좋은 곳 가운데 용신 오행의 "
                             f"기운이 있고 용신 오행의 방위(木 동, 火 남, 土 중앙, 金 서, 水 북)에 가까운 곳을 장소 DB 점수로 골랐습니다.",
                        doc_ids=basis)]
        for idx in sorted(st.travel_picks):
            row, m = st.travel_picks[idx], months[idx]
            parts, ids = [], ["mcp:saju"]
            for scope, label in (("domestic", "국내"), ("overseas", "해외")):
                pick = (row.get(scope) or {}).get("pick")
                if not pick:
                    continue
                match = "·".join(e for e in pick["elements"] if e in ys)
                aspect = f"용신 {match} 기운" if match else f"{pick['elements'][0]} 기운"
                if (pick.get("score_breakdown") or {}).get("direction"):
                    aspect += "·용신 방위"
                parts.append(f"{label} {pick['name']}({pick['direction']}쪽, {aspect})")
                ids.append(pick["id"])
            if not parts:
                continue
            note = " 일지와 충하는 달이라 일정에 여유를 두세요." if m["clash_with_day_branch"] else ""
            claims.append(Claim(section="여행 추천",
                                text=f"{m['month_ganzhi']}월({m['start_date']} ~ {m['end_date']}): {', '.join(parts)}을(를) 추천합니다.{note}",
                                doc_ids=ids))
        return claims if len(claims) > 1 else []

    def _plan_claims(self) -> list[Claim]:
        """evaluate_plan 결과로 계획별 평가 문장을 만든다 (LLM 없이: 절기 월 매핑·등급·근거·대안)."""
        st = self._state()
        if not st.chart:
            return []
        ys = set(st.chart["yongsin_elements"])
        claims = []
        for r in st.plan_results:
            plan = r["plan"]
            what = "이사" if plan["kind"] == "move" else f"{plan['place']} 여행" if plan.get("place") else "여행"
            ids = [d for d in ("mcp:saju", "tool:evaluate_plan", r.get("basis")) if d and d in st.contexts]
            label = f"{plan['year']}년 " if plan.get("year") else ""
            label += f"{plan['month']}월 {what}"
            if r.get("rating") is None:
                claims.append(Claim(section="계획 평가", text=f"{label}{josa(what, '은', '는')} {r['message']}",
                                    doc_ids=ids))
                continue
            span = f"{r['ganzhi']}월({r['start_date']} ~ {r['end_date']})"
            match = "·".join(r["month_yongsin_match"])
            why = [f"그 달 기운이 용신 {match}{josa(match, '과', '와')} 맞습니다." if match
                   else "그 달 기운에는 용신 오행이 없습니다."]
            if r["clash_with_day_branch"]:
                why.append("일지와 충하는 달이라 큰 변화는 신중해야 합니다.")
            if r.get("place_id"):
                place = next(p for p in tools.PLACES if p["id"] == r["place_id"])
                hit = "·".join(e for e in place["elements"] if e in ys)
                kind = f"용신 {hit} 기운이 있는 곳" if hit else "용신 오행과 겹치지 않는 곳"
                why.append(f"{place['name']}{josa(place['name'], '은', '는')} 장소 DB에서 {kind}입니다.")
                if r["place_id"] in st.contexts:
                    ids.append(r["place_id"])
            elif plan.get("place"):
                why.append(f"{plan['place']}{josa(plan['place'], '은', '는')} 장소 DB에 없어 달의 기운만으로 평가했습니다.")
            text = (f"{label}{josa(what, '은', '는')} {span}에 해당하며 평가는 '{r['rating']}'입니다. "
                    + " ".join(why))
            alts = [a for a in r.get("alternatives") or [] if a["id"] in st.contexts][:2]
            if alts:
                text += f" 대안으로 {', '.join(a['name'] for a in alts)}도 고려해 보세요."   # 장소 DB 안의 대안만
                ids += [a["id"] for a in alts]
            if r.get("better_move_months"):
                months = {m["month_ganzhi"]: m for m in st.chart["months"]}
                better = [f"{g}월({months[g]['start_date']} ~ {months[g]['end_date']})" for g in r["better_move_months"][:2]]
                text += f" 이사는 {', '.join(better)}이 더 좋습니다."
            claims.append(Claim(section="계획 평가", text=text, doc_ids=list(dict.fromkeys(ids))))
        return claims

    async def compose(self, s: QueryState) -> QueryState:
        st = self._state()
        ctx = "\n".join(f"- {c['doc_id']}: {c['text'][:160].replace(chr(10), ' ')}" for c in st.context_list())
        feedback = ""
        if s.get("violations"):
            feedback = "\n[이전 답변의 위반 사항 - 반드시 고칠 것]\n" + "\n".join(f"- {v}" for v in s["violations"]) + "\n"
        travel_note = ("19. 월별 여행지 목록(달마다 국내·해외 추천)은 시스템이 '여행 추천' 섹션 끝에 표로 붙인다. "
                       "너는 여행 추천 섹션에 전체 경향을 요약하는 문장 1~2개만 쓰고, 달마다 장소를 나열하지 않는다."
                       if st.travel_picks else "")
        if st.plan_results and self._plan_only():
            travel_note += ("\n20. 계획별 평가(절기 월 매핑, 좋음/보통/주의, 근거, 대안)는 시스템이 '계획 평가' 섹션으로 붙인다. "
                            "너는 계획을 다시 평가하거나 여행지·주거지를 추천하지 말고, 필요하면 '안내' 섹션에 주의점만 쓴다.")
        prompt = COMPOSE_PROMPT.format(feedback=feedback, question=s["masked"], contexts=ctx or "(없음)",
                                       reports=s.get("reports", ""), travel_note=travel_note)
        answer = await self.composer.ainvoke(prompt, {"callbacks": [TraceCallback(st)]})
        return {"answer": answer, "attempts": s.get("attempts", 0) + 1}

    def _plan_only(self) -> bool:
        """계획 평가만 필요한 질문인가 (여행·주거 추천을 함께 묻지 않음)."""
        route = self._state().route
        return "plan_agent" in route and not {"travel_agent", "residence_agent"} & set(route)

    def _claim_violations(self, c: Claim, known: set[str]) -> list[str]:
        out = [f"근거 목록에 없는 doc_id {d}" for d in c.doc_ids if d not in known]
        if "mcp:saju" in known and BIRTH_REQUEST_RE.search(c.text):
            out.append("사주 요약이 있는데 생년월일을 다시 요청함")
        if BIRTH_GUESS_RE.search(c.text):
            out.append("가려진 출생 연·월을 짐작해 씀")
        if (GANZHI_CHARS & set(c.text) or DATE_RE.search(c.text)) and "mcp:saju" in known and "mcp:saju" not in c.doc_ids:
            out.append("간지·기간을 말하는데 mcp:saju 인용 없음")
        place = tools.lookup_place(c.text)
        if place and place["id"] in known and place["id"] not in c.doc_ids and "tool:evaluate_plan" not in c.doc_ids:
            out.append(f"{place['name']}을(를) 말하는데 {place['id']} 인용 없음")
        out += guards.output_violations(c.text)
        return out

    def _repair_month_ranges(self, answer: StructuredAnswer) -> int:
        """'간지월(날짜~날짜)' 표기의 날짜를 사주 요약 값으로 교정한다. 교정한 건수를 돌려준다."""
        chart = self._state().chart
        if not chart:
            return 0
        canon = {m["month_ganzhi"]: f"{m['month_ganzhi']}월({m['start_date']} ~ {m['end_date']})" for m in chart["months"]}
        fixed = 0
        for c in answer.claims:
            def sub(match):
                nonlocal fixed
                right = canon.get(match.group(1))
                if right and match.group(0) != right:
                    fixed += 1
                    return right
                return match.group(0)
            c.text = MONTH_RANGE_RE.sub(sub, c.text)
        return fixed

    async def guard_output(self, s: QueryState) -> QueryState:
        st = self._state()
        fixed = self._repair_month_ranges(s["answer"])
        if fixed:
            st.add_step("guard:output", "month_range_repair", f"fixed={fixed}", type="guard", name="month_range_repair")
        stripped = strip_doc_tags(s["answer"])
        if stripped:
            st.add_step("guard:output", "doc_tag_strip", f"stripped={stripped}", type="guard", name="doc_tag_strip")
        known = set(st.contexts)
        bad = {}
        for i, c in enumerate(s["answer"].claims):
            v = self._claim_violations(c, known)
            if v:
                bad[i] = v
        violations = [f"claim {i + 1} '{s['answer'].claims[i].text[:40]}…': {'; '.join(v)}" for i, v in bad.items()]
        st.add_step("guard:output", f"claims={len(s['answer'].claims)}", f"violations={len(violations)}",
                    type="guard", name="output_guard")
        if violations and s.get("attempts", 0) >= 2:
            # 재생성 후에도 위반이면 해당 문장을 뺀다
            kept = [c for i, c in enumerate(s["answer"].claims) if i not in bad]
            st.add_step("guard:output", "drop", f"removed={len(bad)}", type="guard", name="output_guard")
            return {"answer": StructuredAnswer(claims=kept), "violations": []}
        return {"violations": violations}

    def _note_claims(self) -> list[Claim]:
        """윤달·시각 미상·계절 정의처럼 사주 요약에서 바로 나오는 안내 문장 (LLM 없이)."""
        st = self._state()
        chart, notes = st.chart, []
        if not chart:
            return notes
        if "윤달" in chart["calendar_input"]:
            notes.append(Claim(section="사주 요약", text="음력 윤달 생일을 반영해(평달과 다른 원국으로) 사주를 계산했습니다.",
                               doc_ids=["mcp:saju"]))
        elif chart["calendar_input"].startswith("lunar"):
            notes.append(Claim(section="사주 요약", text="음력 생일(평달)을 양력으로 환산해 사주를 계산했습니다.",
                               doc_ids=["mcp:saju"]))
        if not chart["hour_known"]:
            notes.append(Claim(section="사주 요약", text="출생 시각을 알 수 없어 시주를 제외하고 연·월·일주로 해석했습니다. "
                                                     "용신 해석의 신뢰도가 다소 낮을 수 있습니다.", doc_ids=["mcp:saju"]))
        if st.season:
            ms = [m for m in chart["months"] if m["month_ganzhi"][1] in tools.SEASON_BRANCHES[st.season]]
            span = ", ".join(f"{m['month_ganzhi']}월({m['start_date']} ~ {m['end_date']})" for m in ms)
            notes.append(Claim(section="여행 추천",
                               text=f"명리에서 {st.season}은 {'·'.join(tools.SEASON_BRANCHES[st.season])}월로 보며, "
                                    f"이번 조회 범위에서는 {span}입니다.", doc_ids=["mcp:saju"]))
        return notes

    def _source_claim(self, claims: list[Claim]) -> Claim | None:
        """답변 문장이 인용한 명리 규칙 문서의 제목을 사람이 읽는 형태로 밝힌다 (doc_id 태그 대신)."""
        st = self._state()
        cited = [d for c in claims for d in c.doc_ids if d.startswith("rule:") and d in st.contexts]
        cited = list(dict.fromkeys(cited))
        if not cited:   # 작성자가 인용하지 않았으면 이번 요청에서 검색한 규칙 문서를 밝힌다
            cited = [d for d, c in st.contexts.items() if c["doc_type"] == "rule" and d.startswith("rule:")][:3]
        if not cited:
            return None
        titles = [f"「{st.contexts[d]['text'].splitlines()[0].strip()}」" for d in cited]
        return Claim(section="근거 자료", text=f"해석 근거: 명리 해설 문서 {', '.join(titles)} (교육용으로 작성한 자료)",
                     doc_ids=cited)

    async def finalize(self, s: QueryState) -> QueryState:
        extra = [guards.REFUSAL_MESSAGES[c] for c in s["decision"].partial_categories] if s.get("decision") else []
        claims = list(s["answer"].claims)
        travel = self._travel_claims()
        if travel:
            # 월별 표는 코드가 만들므로 작성자가 쓴 월별·장소 문장은 빼고 요약 문장만 남긴다 (중복·오표기 방지)
            claims = [c for c in claims if not (c.section == "여행 추천" and
                                                (MONTH_RANGE_RE.search(c.text) or tools.lookup_place(c.text, "travel")))]
        plan = self._plan_claims() if self._plan_only() else []
        if plan:
            claims = [c for c in claims if c.section not in PLAN_REPLACED_SECTIONS]
            pos = max((i for i, c in enumerate(claims) if c.section == "사주 요약"), default=-1) + 1
            claims[pos:pos] = plan
        notes = self._note_claims()
        for n in notes:
            pos = max((i for i, c in enumerate(claims) if c.section == n.section), default=-1)
            claims.insert(pos + 1 if pos >= 0 else (0 if n.section == "사주 요약" else len(claims)), n)
        if travel:
            # 작성자가 쓴 여행 요약 문장 바로 뒤(없으면 맨 끝)에 월별 여행 표를 붙인다
            last_travel = max((i for i, c in enumerate(claims) if c.section == "여행 추천"), default=len(claims) - 1)
            claims[last_travel + 1:last_travel + 1] = travel
        source = self._source_claim(claims)
        if source:
            claims.append(source)
        return {"final": render_answer(StructuredAnswer(claims=claims), extra=extra, disclaimer=guards.DISCLAIMER)}

    # ---- 진입점 ----
    async def run_query(self, question: str) -> dict:
        st = start_request()
        try:
            out = await self.graph.ainvoke({"question": question}, {"recursion_limit": 60})
        except (asyncio.TimeoutError, TimeoutError, ReadTimeoutError) as e:
            st.add_step("error", type(e).__name__, ERROR_MESSAGE, type="error", name="timeout")
            out = {"response": ERROR_MESSAGE}
        answer = out.get("response") or out.get("final") or ""
        contexts = [] if out.get("response") else st.context_list()
        return {"answer": answer, "contexts": contexts, "trace": st.trace,
                "total_latency_ms": st.total_latency_ms, "reference_date": config.REFERENCE_DATE}

    async def close(self) -> None:
        await self.hub.close()


async def build_app(use_llm_guard: bool = True) -> SajuPlannerApp:
    hub = await tools.MCPHub().start()
    tools.set_hub(hub)
    return SajuPlannerApp(hub, use_llm_guard=use_llm_guard)


if __name__ == "__main__":
    import asyncio
    import sys

    async def _main(q: str):
        app = await build_app()
        try:
            res = await app.run_query(q)
            print(res["answer"])
            print("\n--- contexts:", [c["doc_id"] for c in res["contexts"]])
            print("--- tools:", [t.get("name") for t in res["trace"] if t.get("type") in ("tool", "retrieval")])
            print(f"--- {res['total_latency_ms'] / 1000:.1f}s")
        finally:
            await app.close()

    asyncio.run(_main(sys.argv[1] if len(sys.argv) > 1 else "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월에 여행 어디로 가면 좋을까?"))
