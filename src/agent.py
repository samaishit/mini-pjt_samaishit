# agent.py - 메인 에이전트 그래프 (LangGraph StateGraph + Supervisor)
"""POST /query 한 번을 처리하는 그래프입니다.

guard_input → saju → supervisor(saju/travel/residence/plan 전문 에이전트) → compose → guard_output → finalize
- guard_input: 제3자 규칙(원문) → 출생 정보 파서 → 규칙 가드 → LLM 판별(<BIRTH> 치환 문장)
  LLM 판별을 기다리는 동안 saju·supervisor 를 미리 돌리고(차단이면 취소·폐기), 그 경우 바로 compose 로 간다
- supervisor: Supervisor LLM 이 맡길 에이전트를 고르고, 라우터의 필수 에이전트와 합쳐 동시에 실행한다
- saju: LLM 없이 사주 MCP 를 직접 호출해 사주 요약을 만든다 (원본 생년월일은 여기까지만)
- compose: 전문 에이전트 보고를 근거가 달린 claims 로 구조화 (LCEL + Pydantic)
- guard_output: doc_id 존재·필수 근거·금칙어 검사, 위반이면 1회 재생성 후 위반 문장 제거
"""
import asyncio
import re
from typing import Any, TypedDict

from botocore.exceptions import ReadTimeoutError

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from src import config, guards, tools
from src.birth_parser import parse
from src.glossary import find_terms
from src.hanja import BRANCH_KO, STEM_KO, annotate
from src.llm import make_llm
from src.schemas import Claim, StructuredAnswer, render_answer, strip_doc_tags
from src.tracing import RequestState, TraceCallback, start_request
from src.tracing import current as current_state

GANZHI_CHARS = set("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# '己亥월(2026-11-07 ~ 2026-12-06)' 형태의 기간 표기 (교정 대상)
MONTH_RANGE_RE = re.compile(r"(?:[가-힣]{2}\()?([甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥])\)?월\s*\(\s*\d{4}-\d{2}-\d{2}\s*[~∼-]\s*(?:\d{4}-)?\d{2}-\d{2}"
                            r"[^)\n]{0,12}\)")
# '임인월, 2027-02-04 ~ 2027-03-05' 처럼 간지를 한글로만 쓴 기간 표기 (그룹 1 = 한글 간지, 그룹 2 = 날짜 범위)
HANGUL_MONTH_RANGE_RE = re.compile(r"([가-힣]{2})월\s*[,(]\s*(\d{4}-\d{2}-\d{2}\s*[~∼-]\s*(?:\d{4}-)?\d{2}-\d{2})")
# 양력 'N월' 표기 (작성자가 절기 월 번호를 양력 월로 잘못 옮기는 경우를 거른다)
SOLAR_MONTH_RE = re.compile(r"(?<![\d-])\d{1,2}\s*월(?!\s*\()")

# 질문 의도 → 반드시 호출할 전문 에이전트 (결정론 키워드 규칙, Supervisor 에 힌트로 주고 누락 시 직접 보완)
PLAN_EVAL_RE = re.compile(r"(\d{1,2}\s*월|내년|올해|이번)[^.?!]{0,30}(여행|이사|캠핑|휴가|등산|트레킹|낚시|나들이|드라이브|출장)"
                          r"[^.?!]{0,25}(괜찮|어때|어떨|해도 될|가도 될|맞아|맞을까|맞나|맞는지|어울)"
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
    "residence_agent": ["rule:residence:selection", "rule:residence:move-timing"],
}
# 사주 요약 문장의 근거 문서 (출생 정보가 있으면 항상 미리 검색 결과에 넣는다)
SUMMARY_RULE_DOCS = ["rule:yongsin:overview", "rule:direction:oheng"]
# 오행별 방위·계절·환경 (rule:direction:oheng 과 같은 내용)
ELEMENT_TRAITS = {"木": "동쪽·봄, 숲과 정원처럼 푸르고 자라나는 곳", "火": "남쪽·여름, 햇볕이 잘 들고 활기 있는 곳",
                  "土": "중앙·환절기, 평지나 오래된 유적처럼 안정된 곳", "金": "서쪽·가을, 바위산처럼 단단하고 맑은 곳",
                  "水": "북쪽·겨울, 바다·강·호수처럼 물이 있는 곳"}
STRENGTH_KO = {"weak": "신약한", "strong": "신강한", "neutral": "중화된", "balanced": "중화된"}
# '용신 水·木' 처럼 용신이라고 말한 오행 (한자 또는 '수(水)' 표기)
YONGSIN_CLAIM_RE = re.compile(r"용신\s*(?:인|은|는|이|의|중|으로)?\s*((?:[가-힣]\()?[木火土金水]\)?(?:\s*[·,]\s*(?:[가-힣]\()?[木火土金水]\)?)*)")
PILLAR_KO = {"year": "연주", "month": "월주", "day": "일주", "hour": "시주"}
# 스트리밍 화면에 보여 줄 전문가 이름
AGENT_LABELS = {"saju_agent": "사주 해석", "travel_agent": "여행", "residence_agent": "주거", "plan_agent": "계획 평가"}
# 전문 에이전트를 동시에 실행할 때 각자에게 주는 담당 범위 (질문 전체를 혼자 답하지 않게)
AGENT_TASKS = {
    "saju_agent": "사주 해석(용신·월운의 의미, 좋은 달·조심할 달)만 보고한다. 여행지·주거지·계획 평가는 다른 에이전트가 맡는다.",
    "travel_agent": "월별 여행지 추천만 맡는다. 먼저 recommend_travel 을 호출하고 그 결과만 보고한다.",
    "residence_agent": "주거 지역 Top 3와 이사 시기만 맡는다. 먼저 recommend_residence 를 호출하고 그 결과만 보고한다.",
    "plan_agent": "사용자가 말한 계획의 평가만 맡는다. 계획마다 evaluate_plan 을 호출하고 그 결과만 보고한다.",
}
# 답변 섹션 표시 순서
SECTION_ORDER = {name: i for i, name in enumerate(
    ["한눈에 보기", "사주 요약", "사주 해석", "월별 운세", "계획 평가", "여행 추천", "주거 추천", "이사 시기", "안내",
     "용어 풀이", "근거 자료"])}
COMPOSE_MAX_TOKENS = 4096   # 답변 작성 출력 상한 (섹션별 claim 수 상한과 함께)
AGENT_MAX_TOKENS = 800    # 사주 해석 에이전트 보고 길이 상한
TOOL_AGENT_MAX_TOKENS = 400   # 여행·주거·계획 에이전트 보고 상한 (표·Top 3·등급 문장은 코드가 도구 결과로 만든다)
# 계획 전용 질문에서 코드가 만든 '계획 평가'로 대체하는 작성자 섹션
PLAN_REPLACED_SECTIONS = {"계획 평가", "여행 추천", "이사 시기", "주거 추천"}


def route_only(state: dict) -> dict:
    """Supervisor 는 첫 판단에서 맡길 전문 에이전트만 고르고 끝낸다. 실행은 run_supervisor 가 동시에 한다.

    langgraph-supervisor 는 넘기기를 하나씩 차례로 실행하고, 에이전트가 보고할 때마다 Supervisor LLM 을 다시 부른다
    (1년 계획 52초의 원인). 고른 에이전트 이름을 요청 상태에 남기고 넘기기를 지워 Supervisor 를 바로 끝낸다.
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    picked = [c["name"].removeprefix("transfer_to_") for c in calls if c["name"].startswith("transfer_to_")]
    current_state().supervisor_pick = list(dict.fromkeys(picked))
    return {"messages": [last.model_copy(update={"tool_calls": [], "content": "완료"})]}


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
    # 읽기 타임아웃으로 무한 대기를 막고, 한도 초과면 config.MODEL_FALLBACKS 순서로 모델을 바꿔 다시 호출한다
    return make_llm(max_tokens=max_tokens)


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
        final = (ord(last) - 0xAC00) % 28
        if with_final == "으로" and final == 8:   # ㄹ 받침 뒤에는 '로' (예: 서울로)
            return without_final
        return with_final if final else without_final
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
- 사주 요약이 있으면 출생 시각 등 정보를 다시 요구하지 말고 그대로 도구를 호출한다(시각 미상은 시주 제외로 이미 반영됨).
- 보고는 한국어 짧은 bullet 8개 이내로, 도구 결과의 핵심 사실만 쓴다(인사말·이모지·표·전체 계획 재작성 금지).
  월별 여행 표·주거 Top 3·계획 등급 문장은 시스템이 도구 결과로 직접 만들므로 다시 나열하지 않는다.
  각 사실 뒤에 근거 doc_id 를 [doc_id] 형태로 붙인다
  (예: [mcp:saju], [rule:yongsin:overview], [T-OS-SAPPORO], [tool:evaluate_plan])."""
# 여행·주거·계획 에이전트: 결과 문장은 시스템이 도구 결과로 만들므로 보고는 해석만 짧게
TOOL_REPORT_RULE = """- 이 에이전트의 보고는 도구 결과를 해석하는 bullet 4개 이내로 쓴다(장소·달 목록을 다시 옮기지 않는다)."""

SAJU_PROMPT = f"""너는 사주 해석 전문가(saju_agent)다. 용신·오행·십성의 의미, 좋은 달과 조심할 달, 특정 기간의 운,
절기 경계 해석을 담당한다. 메시지의 [관련 해석 규칙]에 이미 규칙 문서가 들어 있으니 그것을 인용하고,
retrieve_rule_docs 는 거기에 없는 개념이 꼭 필요할 때만 한 번 호출한다.
좋은 달은 '용신 일치'가 있고 '충'이 없는 달, 조심할 달은 일지 '충'이 있는 달이다.
날짜 구간이 절기 경계에 걸치면 경계 전후를 나누어 설명한다.
{COMMON_RULES}"""

TRAVEL_PROMPT = f"""너는 여행 추천 전문가(travel_agent)다. 월별 여행지는 반드시 recommend_travel 로 고르고, 필요한 달을 모두 담아 한 번만 호출한다.
- 메시지의 [대상 절기 월 번호]를 month_indices 로 그대로 쓴다. 없으면 사주 요약의 '양력 N월' 열에서 질문이 가리키는 달의 번호를 고른다.
- 계절은 명리 기준이다: 봄=寅卯辰월, 여름=巳午未월, 가을=申酉戌월, 겨울=亥子丑월.
- 국내/해외 요청에 맞춰 scope 를 고른다(언급 없으면 both).
각 달의 pick 을 추천하고 이유는 점수 근거(용신 오행·방위·추천 월)로 설명한다. 동행자가 있어도 사용자 본인 사주 기준이라고 밝힌다.
recommend_travel 결과에 장소 설명이 이미 들어 있으므로 get_travel·retrieve_place_docs 는 사용자가 특정 장소를 자세히 물을 때만 부른다.
{COMMON_RULES}
{TOOL_REPORT_RULE}"""

RESIDENCE_PROMPT = f"""너는 주거 추천 전문가(residence_agent)다. 주거 지역과 이사 시기는 반드시 recommend_residence 로 구한다.
Top 3 지역과 그 이유(오행·방위·환경), 이사하기 좋은 달과 피할 달(일지 충)을 보고한다.
투자 판단이 아니라는 점을 한 줄로 밝힌다.
recommend_residence 결과에 지역 설명이 이미 들어 있으므로 get_region·retrieve_place_docs 는 사용자가 특정 지역을 자세히 물을 때만 부른다.
{COMMON_RULES}
{TOOL_REPORT_RULE}"""

PLAN_PROMPT = f"""너는 계획 평가 전문가(plan_agent)다. 사용자가 말한 계획(언제 어디로 여행, 언제 이사 등)마다
evaluate_plan 을 한 번씩 호출한다. '내년'은 기준일 다음 해다. 결과의 rating(좋음/보통/주의), 근거,
대안(alternatives, better_move_months)을 그대로 보고한다. status 가 out_of_window 면 범위 밖이라 평가하지 않는다고 보고한다.
in_db 가 false 면 'DB 외 장소'라 상세 평가를 할 수 없다고 밝히고 대안을 제시한다.
캠핑·휴가·등산처럼 이동하는 활동은 kind="travel", activity 에 활동 이름(예: "캠핑")을 넣고, place 에는 사용자가 말한
장소(예: "강원도")를 그대로 넣는다.
'10월말'처럼 말하면 month=10 이다.
{COMMON_RULES}
{TOOL_REPORT_RULE}"""

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
11. 섹션 이름은 '한눈에 보기', '사주 해석', '월별 운세', '여행 추천', '주거 추천', '이사 시기', '계획 평가', '안내' 중에서 고른다.
    '한눈에 보기'에는 질문에 대한 결론을 claim 1~2개로 먼저 쓴다. 명리 용어(간지·십성·용신·오행 한자)와 YYYY-MM-DD 날짜 없이,
    처음 보는 사람도 바로 알아듣는 일상어로 쓴다(예: "내년 5월은 바깥 활동과 여행에 힘이 실리는 달이라, 숲과 전통 문화가 있는
    동쪽 여행지가 잘 맞습니다."). 달은 사용자가 말한 표현('내년 5월', '10월말')을 쓰고 절기 월 번호를 양력 월로 바꿔 쓰지 않는다.
    한눈에 보기 claim 의 doc_ids 에는 mcp:saju 를 넣는다.
    '사주 요약'(원국·일간·용신·12개월 흐름)은 시스템이 붙이므로 쓰지 않는다. 질문과 관련된 해석(그 달 월운의 십성 의미,
    용신과의 관계)은 '사주 해석' 섹션에 쓴다.
12. claim 은 섹션마다 3개 이내, 전체 12개 이내로 쓴다. 표·목록은 시스템이 붙이므로 같은 내용을 되풀이하지 않는다.
    '당신'이라고 부르지 말고 주어를 생략하거나 '~님'처럼 정중하게 쓴다.
13. 계획 평가 claim 은 '좋음/보통/주의' 등급 이름과 해당 절기 월 기간을 반드시 함께 쓴다.
14. 여러 달을 추천하는 요청이면 보고에 나온 달마다 국내·해외 추천을 빠짐없이 옮긴다.
15. 용신 일치 오행은 사주 요약 표의 '용신 일치' 값 그대로 쓴다. 월운과 용신은 오행으로 비교한다
    (예: '무술(戊戌)월의 토(土) 기운이 용신 토(土)와 맞는다'). 용신 천간 글자가 월운에 들어 있다고 쓰지 않는다.
16. 근거 목록에 mcp:saju 가 있으면 사주 요약이 있는 것이다. 이때 생년월일을 다시 묻지 않는다.
    생년월일은 <BIRTH> 로 가려져 있으므로 출생 연·월('12월생', '1988년생' 등)을 짐작해 쓰지 않는다.
17. 용신·오행·십성의 의미를 묻는 질문에는 rule 문서를 근거로 개념, 사용자 사주에서의 의미, 생활 활용(방위·환경·계절) 예시를 각각 한 문장 이상 쓴다.
18. doc_id 는 doc_ids 필드에만 넣고 text 에는 [doc_id] 같은 태그를 쓰지 않는다.
    대신 해석 근거는 "명리 해설 「용신이란」에 따르면"처럼 문서 제목으로 자연스럽게 밝힌다.
19. 간지·오행은 '무술(戊戌)월', '병오(丙午)년', '토(土)', '병화(丙火)'처럼 한글 읽기 뒤 괄호에 한자를 쓴다. 한자만 단독으로 쓰지 않는다.
20. 질문에 '(이어서 묻는 말)'이 있으면 대화의 이어지는 질문이다. 앞 문장은 맥락이고, 그 뒤의 말에 답하는 것이 우선이다.
    질문에 먼저 답하고, 왜 그런지를 사용자의 용신·그 달 월운(십성)과 장소의 오행·방위·환경으로 연결해 2~3문장으로 설명한다.
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
    preset_chart: dict          # 대화(/chat)에서 기억해 둔 사주 요약 (원본 생년월일 없음)
    prior_answer: str           # 대화(/chat)에서 직전에 보여 준 답변 요약 (같은 해석을 되풀이하지 않게)


class SajuPlannerApp:
    def __init__(self, hub: tools.MCPHub, use_llm_guard: bool = True):
        self.hub = hub
        self.use_llm_guard = use_llm_guard
        self.supervisor = self._build_supervisor()
        self.composer = llm(COMPOSE_MAX_TOKENS).with_structured_output(StructuredAnswer)
        self.graph = self._build_graph()

    # ---- 구성 ----
    def _build_supervisor(self):
        model = llm()
        # 전문 에이전트 보고는 작성자가 다시 정리하므로 짧게 받는다 (긴 보고는 1년 계획에서 180초 초과의 원인)
        agent_model = llm(max_tokens=AGENT_MAX_TOKENS)
        tool_agent_model = llm(max_tokens=TOOL_AGENT_MAX_TOKENS)
        self.agents = {
            "saju_agent": create_react_agent(agent_model, tools.SAJU_TOOLS, prompt=SAJU_PROMPT, name="saju_agent"),
            "travel_agent": create_react_agent(tool_agent_model, tools.TRAVEL_TOOLS, prompt=TRAVEL_PROMPT,
                                               name="travel_agent"),
            "residence_agent": create_react_agent(tool_agent_model, tools.RESIDENCE_TOOLS, prompt=RESIDENCE_PROMPT,
                                                  name="residence_agent"),
            "plan_agent": create_react_agent(tool_agent_model, tools.PLAN_TOOLS, prompt=PLAN_PROMPT, name="plan_agent"),
        }
        return create_supervisor(list(self.agents.values()), model=model, prompt=SUPERVISOR_PROMPT,
                                 output_mode="last_message", post_model_hook=route_only).compile()

    def _build_graph(self):
        g = StateGraph(QueryState)
        g.add_node("guard_input", self.guard_input)
        g.add_node("saju", self.saju)
        g.add_node("supervisor", self.run_supervisor)
        g.add_node("compose", self.compose)
        g.add_node("guard_output", self.guard_output)
        g.add_node("finalize", self.finalize)
        g.set_entry_point("guard_input")
        # 입력 가드 LLM 과 동시에 사주 계산·전문 에이전트를 미리 돌렸으면(reports 있음) 바로 답변 작성으로 간다
        g.add_conditional_edges("guard_input",
                                lambda s: END if s.get("response") else ("compose" if "reports" in s else "saju"))
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
        st.emit("input", "질문과 출생 정보를 확인하고 있어요")
        if guards.check_third_party(s["question"]):
            guards.log_block("third_party", "제3자 규칙")
            st.add_step("guard:input", "(원문 생략)", "block: third_party", type="guard", name="third_party")
            return {"response": guards.REFUSAL_MESSAGES["third_party"]}
        parsed = parse(s["question"], config.REFERENCE_DATE)
        st.add_step("parse:birth", parsed.masked_text,
                    f"birth={'ok' if parsed.birth else 'none'}, clarification={parsed.clarification}",
                    type="parse", name="birth_parser")
        decision = guards.rule_check(parsed.masked_text, parsed.birth is not None)
        use_llm = decision.action == "allow" and self.use_llm_guard
        prepared, snap = None, len(st.trace)
        if use_llm and not parsed.clarification:
            # 입력 가드 LLM(약 2초)을 기다리는 동안 사주 계산과 전문 에이전트를 미리 시작한다.
            # 차단이면 취소하고 그 사이의 trace·사주·근거를 버린다 (답변·contexts 로는 나가지 않음)
            prepared = asyncio.create_task(self._prepare({**s, "masked": parsed.masked_text, "birth": parsed.birth}))
        if use_llm:
            try:
                # 입력 가드 LLM 은 동기 호출이라 별도 스레드에서 돌린다 (이벤트 루프를 막으면 스트리밍 알림이 늦어진다)
                decision = await asyncio.to_thread(guards.llm_guard, parsed.masked_text, decision)
            except BaseException:
                if prepared:
                    prepared.cancel()
                raise
        st.add_step("guard:input", parsed.masked_text, f"{decision.action}: {decision.category or decision.partial_categories}",
                    type="guard", name="input_guard")
        if decision.action == "block":
            if prepared:
                await self._discard_prepared(prepared, snap)
            guards.log_block(decision.category, decision.reason)
            return {"response": decision.message}
        if parsed.clarification:
            return {"response": parsed.clarification_message}
        out = {"masked": parsed.masked_text, "birth": parsed.birth, "decision": decision}
        if prepared:
            out.update(await prepared)
        return out

    async def _prepare(self, s: QueryState) -> QueryState:
        """사주 계산 → 전문 에이전트 (입력 가드 LLM 과 동시에 돌리는 부분)."""
        await self.saju(s)
        return await self.run_supervisor(s)

    async def _discard_prepared(self, task: asyncio.Task, snap: int) -> None:
        """차단된 요청의 미리 돌린 작업을 취소하고 그 결과를 요청 상태에서 지운다."""
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        st = self._state()
        st.trace[snap:] = [t for t in st.trace[snap:] if t.get("name") == "input_guard"]
        st.chart, st.residence = None, None
        st.contexts.clear()
        st.travel_picks.clear()
        st.plan_results.clear()
        st.date_spans.clear()
        st.route, st.supervisor_pick = [], []

    async def saju(self, s: QueryState) -> QueryState:
        if s.get("birth"):
            self._state().emit("saju", "만세력으로 사주와 앞으로 12개월의 운을 계산하고 있어요")
            await tools.compute_chart(s["birth"], config.REFERENCE_DATE)
        elif s.get("preset_chart"):
            self._state().emit("saju", "대화에서 기억해 둔 사주를 불러왔어요")
            # 대화 중 이전 턴에서 계산한 사주 요약을 다시 쓴다 (생년월일을 다시 받지 않음)
            st = self._state()
            st.chart = s["preset_chart"]
            st.add_context("mcp:saju", tools.chart_text(st.chart), "tool", "saju")
            st.add_step("memory:chart", "세션에 기억된 사주 요약", "reuse", type="memory", name="session_chart")
        return {}

    async def run_supervisor(self, s: QueryState) -> QueryState:
        st = self._state()
        chart_block = tools.chart_text(st.chart) if st.chart else "(출생 정보 없음: 사주 요약이 없습니다)"
        need = required_agents(s["masked"])
        st.route = need
        st.emit("route", "질문에 맞는 전문가를 골랐어요: " + ", ".join(AGENT_LABELS.get(a, a) for a in need), agents=need)
        st.travel_scope = tools.travel_scope(s["masked"])
        indices = tools.target_month_indices(s["masked"])
        content = (f"[기준일] {config.REFERENCE_DATE}\n[사주 요약 · doc_id=mcp:saju]\n{chart_block}\n\n"
                   f"[사용자 질문]\n{s['masked']}\n\n[필수 호출 에이전트] {', '.join(need)}")
        if st.travel_scope in ("domestic", "overseas"):
            content += f"\n[여행 범위] {'국내만' if st.travel_scope == 'domestic' else '해외만'} (recommend_travel scope={st.travel_scope})"
        if indices:
            # 번호만 주면 양력 월로 오해하므로 간지와 기간을 함께 적는다
            months = {m["index"]: m for m in st.chart["months"]}
            labels = ", ".join(f"{i}번={months[i]['month_ganzhi']}월({months[i]['start_date']} ~ {months[i]['end_date']})"
                               for i in indices)
            content += f"\n[대상 절기 월 번호] {indices} → {labels} (절기 월 번호이며 양력 월이 아님)"
        spans = tools.split_date_spans(s["masked"])
        if spans:
            content += "\n[날짜 구간의 절기 월 (절입일 기준, 그대로 인용)]\n" + "\n".join(self._span_text(sp) for sp in spans)
        extra = [d for name, ids in RULE_DOCS_BY_AGENT.items() if name in need for d in ids]
        if st.chart:   # 코드가 붙이는 사주 요약(용신 의미·오행 방위)의 근거
            extra += [d for d in SUMMARY_RULE_DOCS if d not in extra]
        if "saju_agent" in need or extra:
            # 해석 근거가 필요한 질문은 규칙 문서를 먼저 검색해 에이전트에게 함께 넘긴다 (RAG)
            rules = tools.pre_retrieve_rules(s["masked"], extra_ids=extra)
            content += "\n\n[관련 해석 규칙]\n" + "\n".join(f"- [{r['doc_id']}] {r['text']}" for r in rules)
        st.add_step("route", s["masked"], {"agents": need, "month_indices": indices}, type="route", name="intent_router")
        callbacks = {"callbacks": [TraceCallback(st)], "recursion_limit": 40}
        # Supervisor LLM 은 맡길 에이전트를 한 번 고르고 끝난다 (route_only). 라우터의 필수 에이전트와 합쳐 동시에 실행한다
        st.supervisor_pick = []
        await self.supervisor.ainvoke({"messages": [HumanMessage(content=content)]}, callbacks)
        picked = [name for name in st.supervisor_pick if name in self.agents]
        names = need + [name for name in picked if name not in need]

        async def run_agent(name: str) -> str | None:
            by = "supervisor" if name in picked else "router"   # router = Supervisor 가 건너뛴 필수 에이전트 보완
            st.add_step(f"agent:{name}", by, "handoff", type="agent", name=name)
            # 동시에 실행하므로 다른 에이전트의 몫까지 쓰지 않게 맡은 범위를 적어 준다
            task = f"{content}\n\n[이 에이전트의 담당] {AGENT_TASKS[name]}"
            sub = await self.agents[name].ainvoke({"messages": [HumanMessage(content=task)]}, callbacks)
            last = next((m for m in reversed(sub["messages"]) if isinstance(m, AIMessage) and get_text(m).strip()), None)
            return f"## {name}\n{get_text(last)}" if last else None

        reports = [r for r in await asyncio.gather(*(run_agent(n) for n in names)) if r]
        # 여행 추천이 필요한데 월별 추천 도구가 불리지 않았으면 규칙대로 직접 계산한다
        if "travel_agent" in need and st.chart and not st.travel_picks:
            scope = st.travel_scope or "both"   # 이어 묻는 말에 국내/해외가 있으면 그것을 따른다
            await tools.recommend_travel.ainvoke({"month_indices": indices or list(range(1, 13)), "scope": scope})
            st.add_step("agent:travel_agent", "fallback", "recommend_travel 직접 호출", type="agent", name="travel_agent")
        # 주거 추천이 필요한데 recommend_residence 가 불리지 않았으면 규칙대로 직접 계산한다
        if "residence_agent" in need and st.chart and st.residence is None:
            await tools.recommend_residence.ainvoke({})
            st.add_step("agent:residence_agent", "fallback", "recommend_residence 직접 호출", type="agent",
                        name="residence_agent")
        return {"reports": "\n\n".join(reports) or "(보고 없음)"}

    def _travel_claims(self) -> list[Claim]:
        """recommend_travel 결과로 월별 여행 추천 문장을 만든다 (LLM 없이, 근거 doc_id 포함)."""
        st = self._state()
        if not st.chart or not st.travel_picks:
            return []
        months = {m["index"]: m for m in st.chart["months"]}
        basis = ["mcp:saju"] + [d for d in ("rule:direction:oheng",) if d in st.contexts]
        ys = "·".join(st.chart["yongsin_elements"])
        claims = [Claim(section="여행 추천",
                        text=f"필요한 기운(용신)이 {ys}이라, 그 기운이 담긴 곳이나 그 기운의 방향(木은 동쪽, 火는 남쪽, "
                             f"土는 중앙, 金은 서쪽, 水는 북쪽)에 있는 곳을 추천 여행지 목록에서 골랐습니다.",
                        doc_ids=basis)]
        for n, idx in enumerate(sorted(st.travel_picks)):
            row, m = st.travel_picks[idx], months[idx]
            dom = (row.get("domestic") or {}).get("pick")
            ovs = (row.get("overseas") or {}).get("pick")
            if not dom and not ovs:
                continue
            when = f"{m['month_ganzhi']}월({m['start_date']} ~ {m['end_date']})"
            sentences = []
            if dom:
                lead = f"{when}에는 국내 {dom['name']}{josa(dom['name'], '을', '를')} 추천합니다. 이곳은"
                sentences.append(self._place_sentence(dom["id"], lead=lead))
            if ovs:
                if dom:   # 두 가지 말투를 번갈아 써서 12개월 표가 같은 문장으로 반복되지 않게 한다
                    lead = (f"해외라면 {ovs['name']}{josa(ovs['name'], '이', '가')} 좋습니다. 이곳은" if n % 2 == 0
                            else f"해외로는 {ovs['name']}{josa(ovs['name'], '을', '를')} 꼽았습니다. 이곳은")
                else:
                    lead = f"{when}에는 해외 {ovs['name']}{josa(ovs['name'], '을', '를')} 추천합니다. 이곳은"
                sentences.append(self._place_sentence(ovs["id"], lead=lead))
            if m["clash_with_day_branch"]:
                sentences.append("다만 이 달은 내 기운과 부딪히는(일지 충) 달이라 일정에 여유를 두세요.")
            ids = ["mcp:saju"] + [p["id"] for p in (dom, ovs) if p]
            claims.append(Claim(section="여행 추천", text=" ".join(sentences), doc_ids=ids))
        return claims if len(claims) > 1 else []

    def _residence_claims(self) -> list[Claim]:
        """recommend_residence 결과로 주거 Top 3·이사 시기 문장을 만든다 (LLM 없이, 시세·투자 표현 없음)."""
        st = self._state()
        res = st.residence
        if not st.chart or not res:
            return []
        months = {m["month_ganzhi"]: m for m in st.chart["months"]}
        basis = [d for d in ("mcp:saju", "rule:residence:selection") if d in st.contexts]
        claims = []
        for rank, r in enumerate(res["top3"], 1):
            lead = f"{rank}위 {r['name']}{josa(r['name'], '은', '는')}"
            claims.append(Claim(section="주거 추천", text=self._place_sentence(r["id"], lead=lead),
                                doc_ids=basis + ([r["id"]] if r["id"] in st.contexts else [])))
        span = lambda g: f"{g}월({months[g]['start_date']} ~ {months[g]['end_date']})"
        move_ids = [d for d in ("mcp:saju", "rule:residence:move-timing") if d in st.contexts]
        if res["good"]:
            claims.append(Claim(section="이사 시기",
                                text=f"이사는 필요한 기운이 들어오는 {', '.join(span(g) for g in res['good'])}에 하면 좋습니다.",
                                doc_ids=move_ids))
        if res["avoid"]:
            avoid = ", ".join(span(a) for a in res["avoid"])
            claims.append(Claim(section="이사 시기",
                                text=f"{avoid}{josa(avoid[-1], '은', '는')} 내 기운과 부딪히는(일지 충) 달이라 이사는 피하는 편이 좋습니다.",
                                doc_ids=move_ids))
        return claims

    @staticmethod
    def _place_sentence(place_id: str, month: int | None = None, lead: str | None = None) -> str:
        """장소가 맞는 이유를 한 문장으로 (장소 DB 값만): 특징·방향·필요한 기운·제철."""
        st = current_state()
        place = next(p for p in tools.PLACES if p["id"] == place_id)
        hit = "·".join(e for e in place["elements"] if e in set(st.chart["yongsin_elements"]))
        name = place["name"]
        kind = "여행지" if place["kind"] == "travel" else "지역"
        feat = "·".join((place.get("env_tags") or [])[:2])
        where = f"{feat}{josa(feat, '이', '가')} 있는 {place['direction']}쪽 {kind}" if feat else f"{place['direction']}쪽 {kind}"
        head = lead or f"{name}{josa(name, '은', '는')}"
        text = (f"{head} {where}{josa(where, '으로', '로')}, 필요한 기운 {hit}{josa(hit, '을', '를')} 채우기 좋습니다." if hit
                else f"{head} {where}지만 필요한 기운과는 결이 다릅니다.")
        best = place.get("best_months") or []
        if month and best:
            text += f" {month}월이 이곳을 즐기기 좋은 때입니다." if month in best else \
                f" 이곳은 {', '.join(map(str, best))}월에 가장 좋습니다."
        return text

    def _summary_claims(self) -> list[Claim]:
        """사주 요약(원국·일간·용신과 그 의미·세운·12개월 흐름)을 사주 계산 결과로 만든다 (LLM 없이)."""
        st = self._state()
        ch = st.chart
        if not ch:
            return []
        saju = ["mcp:saju"]
        pillars = ", ".join(f"{PILLAR_KO.get(k, k)} {v}" for k, v in ch["pillars"].items())
        day_el = tools.STEM_ELEMENT[ch["day_stem"]]
        claims = [Claim(section="사주 요약",
                        text=f"원국(타고난 사주 바탕)은 {pillars}입니다. '나'를 뜻하는 일간은 {ch['day_stem']}{day_el}이고 "
                             f"일지는 {ch['day_branch']}이며, {STRENGTH_KO.get(ch['strength'], ch['strength'] + '한')} 사주로 "
                             f"{ch['geukguk']}입니다.",
                        doc_ids=saju)]
        ys = [f"{s}{tools.STEM_ELEMENT[s]}" for s in ch["yongsin"]]
        traits = "; ".join(f"{e}{josa(e, '은', '는')} {ELEMENT_TRAITS[e]}" for e in ch["yongsin_elements"])
        basis = saju + [d for d in SUMMARY_RULE_DOCS if d in st.contexts]
        claims.append(Claim(section="사주 요약",
                            text=f"용신은 {', '.join(ys)}입니다. 용신은 내 사주에서 넘치거나 부족한 기운을 맞춰 주는 "
                                 f"'나에게 필요한 기운'이라, 그 기운이 있는 곳과 때를 가까이하면 좋습니다: {traits}.",
                            doc_ids=basis))
        months = ch["months"]
        years = list(dict.fromkeys(m["year_ganzhi"] for m in months))
        year_txt = []
        for y in years:
            hit = [e for e in dict.fromkeys((tools.STEM_ELEMENT[y[0]], tools.BRANCH_ELEMENT[y[1]])) if e in ch["yongsin_elements"]]
            year_txt.append(f"{y}년" + (f"(필요한 기운 {'·'.join(hit)}{josa(hit[-1], '과', '와')} 맞는 해)" if hit else ""))
        good = [m for m in months if m["good_month"]]
        clash = [m for m in months if m["clash_with_day_branch"]]
        flow = (f"앞으로 1년({months[0]['start_date']} ~ {months[-1]['end_date']})은 {', '.join(year_txt)}의 흐름에 걸칩니다. "
                f"12개 절기 월 가운데 {len(good)}개월이 필요한 기운이 들어오는 좋은 달이고, "
                + (f"조심할 달은 {', '.join(m['month_ganzhi'] + '월(' + m['start_date'] + ' ~ ' + m['end_date'] + ')' for m in clash)}"
                   f"{'로, 내 기운과 부딪히는(일지 충) 달입니다.' if len(clash) == 1 else '입니다. 모두 내 기운과 부딪히는(일지 충) 달입니다.'}"
                   if clash else "내 기운과 부딪히는(일지 충) 달은 없습니다."))
        claims.append(Claim(section="사주 요약", text=flow, doc_ids=saju))
        return claims

    def _plan_claims(self) -> list[Claim]:
        """evaluate_plan 결과로 계획별 평가 문장을 만든다 (LLM 없이: 절기 월 매핑·등급·근거·대안)."""
        st = self._state()
        if not st.chart:
            return []
        ys = set(st.chart["yongsin_elements"])
        claims = []
        for r in st.plan_results:
            plan = r["plan"]
            act = plan.get("activity") or ("이사" if plan["kind"] == "move" else "여행")
            what = f"{plan['place']} {act}" if plan.get("place") else act
            ids = [d for d in ("mcp:saju", "tool:evaluate_plan", r.get("basis")) if d and d in st.contexts]
            label = f"{plan['year']}년 " if plan.get("year") else ""
            label += f"{plan['month']}월 {what}"
            if r.get("rating") is None:
                claims.append(Claim(section="계획 평가", text=f"{label}{josa(what, '은', '는')} {r['message']}",
                                    doc_ids=ids))
                continue
            span = f"{r['ganzhi']}월({r['start_date']} ~ {r['end_date']})"
            match = "·".join(r["month_yongsin_match"])
            why = [f"그달에는 필요한 기운 {match}{josa(match, '이', '가')} 들어옵니다." if match
                   else "그달에는 필요한 기운이 들어오지 않습니다."]
            if r["clash_with_day_branch"]:
                why.append(f"{'다만' if match else '또'} 내 기운과 부딪히는(일지 충) 달이라 큰 일정은 여유 있게 잡으세요.")
            if r.get("place_id"):
                why.append(self._place_sentence(r["place_id"], plan["month"]))
                if r["place_id"] in st.contexts:
                    ids.append(r["place_id"])
            elif r.get("province"):
                why.append(f"{r['province']}{josa(r['province'], '은', '는')} 한 곳의 여행지가 아니라 지역이라 그달의 기운으로 평가했습니다.")
            if plan.get("place") and not r.get("place_id") and not r.get("province"):
                # DB 에 없는 장소는 평가하지 않는다: 장소 평가를 하지 않았다는 말을 먼저, 등급은 '시기'에 대해서만
                place_name = plan["place"]
                text = (f"{label}: {place_name}{josa(place_name, '은', '는')} 추천 여행지 목록에 없어 장소 자체는 평가하지 "
                        f"않았습니다. 시기만 보면 {span}이고, 시기 평가는 '{r['rating']}'입니다. " + " ".join(why))
            else:
                text = (f"{label}{josa(what, '은', '는')} {span}에 해당하고, 평가는 '{r['rating']}'입니다. "
                        + " ".join(why))
            alts = [a for a in r.get("alternatives") or [] if a["id"] in st.contexts][:2]
            if alts:   # 추천 여행지 목록 안의 대안만, 고른 이유와 함께
                lead = f" {r['province']} 안에서 고른다면" if r.get("province") else " 대신"
                text += lead + " " + " ".join(self._place_sentence(a["id"], plan["month"]) for a in alts)
                ids += [a["id"] for a in alts]
            if r.get("better_move_months"):
                months = {m["month_ganzhi"]: m for m in st.chart["months"]}
                better = [f"{g}월({months[g]['start_date']} ~ {months[g]['end_date']})" for g in r["better_move_months"][:2]]
                text += f" 이사는 {', '.join(better)}에 하는 편이 더 좋습니다."
            claims.append(Claim(section="계획 평가", text=text, doc_ids=list(dict.fromkeys(ids))))
        return claims

    async def compose(self, s: QueryState) -> QueryState:
        st = self._state()
        st.emit("compose", "근거를 보강해 답변을 다시 정리하고 있어요" if s.get("attempts") else "전문가 보고를 모아 답변을 정리하고 있어요")
        ctx = "\n".join(f"- {c['doc_id']}: {c['text'][:160].replace(chr(10), ' ')}" for c in st.context_list())
        feedback = ""
        if s.get("violations"):
            feedback = "\n[이전 답변의 위반 사항 - 반드시 고칠 것]\n" + "\n".join(f"- {v}" for v in s["violations"]) + "\n"
        travel_note = ("21. 월별 여행지 목록(달마다 국내·해외 추천)은 시스템이 '여행 추천' 섹션 끝에 표로 붙인다. "
                       "너는 여행 추천 섹션에 전체 경향을 요약하는 문장 1~2개만 쓰고, 달마다 장소를 나열하지 않는다."
                       if st.travel_picks else "")
        if st.plan_results and self._plan_only():
            travel_note += ("\n22. 계획별 평가(절기 월 매핑, 좋음/보통/주의, 근거, 장소 특징, 대안)는 시스템이 '계획 평가' 섹션으로 붙인다. "
                            "너는 계획을 다시 평가하거나 여행지·주거지를 추천하지 말고, '사주 해석' 섹션에 그 달 월운(십성)이 "
                            "그 활동과 어떻게 어울리는지를, 필요하면 '안내' 섹션에 주의점을 쓴다. "
                            "장소가 DB 에 있는지는 계획 평가 문장이 이미 밝히므로 다시 쓰지 않는다.")
            unknown = [r["plan"]["place"] for r in st.plan_results
                       if r["plan"].get("place") and not r.get("place_id") and not r.get("province")]
            if unknown:
                travel_note += (f"\n23. 계획 장소 {', '.join(unknown)}은(는) 추천 장소 DB에 없다. 한눈에 보기를 포함한 어떤 문장에서도 "
                                "이 장소가 좋다·주의하라고 평가하지 말고, 'DB에 없어 장소는 평가하지 않는다'를 먼저 밝힌 뒤 "
                                "시기(그 달의 기운)와 DB 안의 대안만 말한다. '조회 범위 밖'은 날짜에만 쓰는 말이므로 장소에 쓰지 않는다. "
                                "다른 달을 권할 때는 양력 월 숫자를 지어내지 말고 사주 요약에 적힌 간지월과 기간을 쓴다.")
        if s.get("prior_answer"):
            travel_note += ("\n24. 대화의 이어 묻기다. 아래 [직전 답변 요약]에서 이미 말한 해석·추천은 되풀이하지 말고, "
                            "'(이어서 묻는 말)'에 새로 물은 것에 집중한다. 같은 사실이 꼭 필요하면 한 문장으로만 짚는다.\n"
                            f"[직전 답변 요약]\n{s['prior_answer']}")
        prompt = COMPOSE_PROMPT.format(feedback=feedback, question=s["masked"], contexts=ctx or "(없음)",
                                       reports=s.get("reports", ""), travel_note=travel_note)
        answer = await self.composer.ainvoke(prompt, {"callbacks": [TraceCallback(st)]})
        return {"answer": answer, "attempts": s.get("attempts", 0) + 1}

    @staticmethod
    def _span_text(sp: dict) -> str:
        """날짜 구간을 절기 월별로 나눈 안내 문장 (절입일 기준)."""
        def md(iso: str) -> str:
            return f"{int(iso[5:7])}월 {int(iso[8:10])}일"
        pieces = [f"{md(p['from'])}~{md(p['to'])}은 {p['month_ganzhi']}월({p['start_date']} ~ {p['end_date']})" for p in sp["parts"]]
        if len(sp["parts"]) == 1:
            return f"{md(sp['from'])}~{md(sp['to'])}은 모두 {sp['parts'][0]['month_ganzhi']}월" \
                   f"({sp['parts'][0]['start_date']} ~ {sp['parts'][0]['end_date']})에 속합니다."
        turns = ", ".join(f"{md(p['start_date'])}" for p in sp["parts"][1:])
        return (f"절기 월은 양력 1일이 아니라 절입일({turns})에 바뀌므로 이 기간은 두 절기 월에 걸칩니다: "
                + ", ".join(pieces) + "입니다.")

    def _unknown_place_headline(self) -> Claim | None:
        """계획 장소가 DB 에 없을 때의 한눈에 보기 결론 (장소는 평가하지 않고 시기와 DB 안 대안만)."""
        st = self._state()
        for r in st.plan_results:
            plan = r["plan"]
            if not plan.get("place") or r.get("place_id") or r.get("province") or r.get("rating") is None:
                continue
            place = plan["place"]
            text = (f"{place}{josa(place, '은', '는')} 추천 여행지 목록에 없어 장소 자체는 평가하지 않았습니다. "
                    f"{plan['month']}월이라는 시기만 보면 '{r['rating']}'입니다.")
            alts = [a["name"] for a in r.get("alternatives") or [] if a["id"] in st.contexts][:2]
            if alts:
                text += f" 같은 시기라면 {', '.join(alts)}처럼 목록에 있는 여행지가 잘 맞습니다."
            ids = [d for d in ("mcp:saju", "tool:evaluate_plan") if d in st.contexts]
            ids += [a["id"] for a in r.get("alternatives") or [] if a["id"] in st.contexts][:2]
            return Claim(section="한눈에 보기", text=text, doc_ids=ids)
        return None

    def _glossary_claims(self, claims: list[Claim]) -> list[Claim]:
        """답변에 나온 명리 용어를 일상어로 풀이한다 (src/glossary.py, 근거는 해당 규칙 문서)."""
        st = self._state()
        if not st.chart:
            return []
        terms = find_terms("\n".join(c.text for c in claims), st.chart.get("geukguk"))
        if not terms:
            return []
        known = set(tools.attach_rule_docs([doc for _, _, doc in terms], "용어 풀이: " + ", ".join(n for n, _, _ in terms)))
        return [Claim(section="용어 풀이", text=f"{name}: {plain}", doc_ids=[doc])
                for name, plain, doc in terms if doc in known]

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
        chart = self._state().chart
        if chart:
            ys = set(chart["yongsin_elements"])
            said = {e for m in YONGSIN_CLAIM_RE.finditer(c.text) for e in re.findall(r"[木火土金水]", m.group(1))}
            if said - ys:
                out.append(f"용신이 아닌 오행({'·'.join(sorted(said - ys))})을 용신이라고 씀")
        if (GANZHI_CHARS & set(c.text) or DATE_RE.search(c.text)) and "mcp:saju" in known and "mcp:saju" not in c.doc_ids:
            out.append("간지·기간을 말하는데 mcp:saju 인용 없음")
        place = tools.lookup_place(c.text)
        if place and place["id"] in known and place["id"] not in c.doc_ids and "tool:evaluate_plan" not in c.doc_ids:
            out.append(f"{place['name']}을(를) 말하는데 {place['id']} 인용 없음")
        out += guards.output_violations(c.text)
        return out

    @staticmethod
    def _repair_citations(answer: StructuredAnswer, known: set[str]) -> int:
        """근거는 contexts 에 있는데 doc_id 만 빠진 경우 코드가 붙인다 (LLM 재생성 대신, 결정론).

        - 간지·기간을 말하는데 mcp:saju 가 없으면 → mcp:saju
        - DB 장소 이름을 말하는데 그 장소 id 가 없으면 → 장소 id (도구가 돌려준 장소만, 즉 contexts 에 있을 때만)
        근거가 contexts 에 없는 경우와 금칙어·용신 오기 같은 내용 위반은 그대로 검사에 걸린다.
        """
        added = 0
        for c in answer.claims:
            need = []
            if "mcp:saju" in known and "mcp:saju" not in c.doc_ids and (GANZHI_CHARS & set(c.text) or DATE_RE.search(c.text)):
                need.append("mcp:saju")
            need += [p["id"] for p in tools.find_places(c.text) if p["id"] in known and p["id"] not in c.doc_ids]
            if need:
                c.doc_ids = list(dict.fromkeys(c.doc_ids + need))
                added += len(need)
        return added

    def _repair_month_ranges(self, answer: StructuredAnswer) -> int:
        """'간지월(날짜~날짜)' 표기의 날짜를 사주 요약 값으로 교정한다. 교정한 건수를 돌려준다.

        간지를 한글로만 쓴 '임인월, 날짜 ~ 날짜' 표기도 사주 요약의 12개월 안에서 읽기가 같으면 날짜만 교정한다.
        """
        chart = self._state().chart
        if not chart:
            return 0
        canon = {m["month_ganzhi"]: f"{m['month_ganzhi']}월({m['start_date']} ~ {m['end_date']})" for m in chart["months"]}
        by_reading = {STEM_KO[m["month_ganzhi"][0]] + BRANCH_KO[m["month_ganzhi"][1]]: f"{m['start_date']} ~ {m['end_date']}"
                      for m in chart["months"]}
        fixed = 0
        for c in answer.claims:
            def sub(match):
                nonlocal fixed
                right = canon.get(match.group(1))
                if not right:
                    return match.group(0)
                if DATE_RE.findall(match.group(0)) != DATE_RE.findall(right):   # 날짜가 다를 때만 교정으로 센다
                    fixed += 1
                return right

            def sub_reading(match):
                nonlocal fixed
                right = by_reading.get(match.group(1))
                if not right:
                    return match.group(0)
                if DATE_RE.findall(match.group(2)) != DATE_RE.findall(right):
                    fixed += 1
                return match.group(0)[:match.start(2) - match.start(0)] + right
            c.text = MONTH_RANGE_RE.sub(sub, c.text)
            c.text = HANGUL_MONTH_RANGE_RE.sub(sub_reading, c.text)
        return fixed

    async def guard_output(self, s: QueryState) -> QueryState:
        st = self._state()
        st.emit("validate", "근거와 표현을 검증하고 있어요")
        fixed = self._repair_month_ranges(s["answer"])
        if fixed:
            st.add_step("guard:output", "month_range_repair", f"fixed={fixed}", type="guard", name="month_range_repair")
        stripped = strip_doc_tags(s["answer"])
        if stripped:
            st.add_step("guard:output", "doc_tag_strip", f"stripped={stripped}", type="guard", name="doc_tag_strip")
        known = set(st.contexts)
        cited = self._repair_citations(s["answer"], known)
        if cited:
            st.add_step("guard:output", "citation_repair", f"added={cited}", type="guard", name="citation_repair")
        bad = {}
        for i, c in enumerate(s["answer"].claims):
            v = self._claim_violations(c, known)
            if v:
                bad[i] = v
        partial = getattr(s.get("decision"), "partial_categories", None) or []
        refused = {i for i, v in bad.items() if all(x.startswith("부동산 투자 표현") for x in v)}
        if "real_estate" in partial and refused:
            # 부동산 부분 거절 문구는 코드가 붙인다. 시세를 다시 언급한 문장은 재생성해도 되풀이되므로(5~6초) 바로 뺀다
            s["answer"].claims = [c for i, c in enumerate(s["answer"].claims) if i not in refused]
            st.add_step("guard:output", "partial_refusal", f"removed={len(refused)}", type="guard", name="partial_refusal_drop")
            bad = {i: v for i, c in enumerate(s["answer"].claims) if (v := self._claim_violations(c, known))}
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
        more = f" 외 {len(titles) - 3}건" if len(titles) > 3 else ""
        return Claim(section="근거 자료",
                     text=f"해석 근거는 명리 해설 문서 {', '.join(titles[:3])}{more}입니다(교육용으로 직접 작성한 자료).",
                     doc_ids=cited)

    async def finalize(self, s: QueryState) -> QueryState:
        self._state().emit("finalize", "월별 표와 용어 풀이를 붙여 답변을 완성하고 있어요")
        extra = [guards.REFUSAL_MESSAGES[c] for c in s["decision"].partial_categories] if s.get("decision") else []
        claims = list(s["answer"].claims)
        travel = self._travel_claims()
        if travel:
            # 월별 표는 코드가 만들므로 작성자가 쓴 월별·장소 문장은 빼고 요약 문장만 남긴다 (중복·오표기 방지)
            claims = [c for c in claims if not (c.section == "여행 추천" and
                                                (MONTH_RANGE_RE.search(c.text) or tools.lookup_place(c.text, "travel")
                                                 or SOLAR_MONTH_RE.search(c.text)))]
        residence = self._residence_claims() if "residence_agent" in self._state().route else []
        if residence:
            claims = [c for c in claims if not (
                (c.section == "주거 추천" and tools.lookup_place(c.text, "region")) or
                (c.section == "이사 시기" and (MONTH_RANGE_RE.search(c.text) or SOLAR_MONTH_RE.search(c.text))))]
            claims += residence
        plan = self._plan_claims() if self._plan_only() else []
        headline = self._unknown_place_headline()
        if headline:
            # DB 에 없는 장소를 평가하는 듯한 작성자 결론은 빼고, '장소는 평가하지 않음'을 먼저 밝히는 결론으로 바꾼다
            names = [r["plan"]["place"] for r in self._state().plan_results if r["plan"].get("place")]
            claims = [c for c in claims if not (c.section == "한눈에 보기" and any(n in c.text for n in names))]
            claims.insert(0, headline)
        if plan:
            claims = [c for c in claims if c.section not in PLAN_REPLACED_SECTIONS]
            pos = max((i for i, c in enumerate(claims) if c.section == "사주 요약"), default=-1) + 1
            claims[pos:pos] = plan
        spans = [Claim(section="월별 운세", text=self._span_text(sp), doc_ids=["mcp:saju"])
                 for sp in self._state().date_spans]
        if spans:
            pos = next((i for i, c in enumerate(claims) if c.section == "월별 운세"), None)
            if pos is None:
                pos = max((i for i, c in enumerate(claims) if c.section == "사주 요약"), default=-1) + 1
            claims[pos:pos] = spans
        summary = self._summary_claims()
        if summary:
            for c in claims:
                if c.section == "사주 요약":
                    c.section = "사주 해석"
            claims[0:0] = summary
        notes = self._note_claims()
        for n in notes:
            pos = max((i for i, c in enumerate(claims) if c.section == n.section), default=-1)
            claims.insert(pos + 1 if pos >= 0 else (0 if n.section == "사주 요약" else len(claims)), n)
        if travel:
            # 작성자가 쓴 여행 요약 문장 바로 뒤(없으면 맨 끝)에 월별 여행 표를 붙인다
            last_travel = max((i for i, c in enumerate(claims) if c.section == "여행 추천"), default=len(claims) - 1)
            claims[last_travel + 1:last_travel + 1] = travel
        # 섹션은 정해진 순서로 보여 준다 (같은 섹션 안의 순서는 유지)
        claims.sort(key=lambda c: SECTION_ORDER.get(c.section, SECTION_ORDER["안내"] - 0.5))
        claims += self._glossary_claims(claims)
        source = self._source_claim(claims)
        if source:
            claims.append(source)
        claims = self._check_code_claims(claims, {id(c) for c in s["answer"].claims})
        # 검증은 한자 원문으로 마쳤으므로, 보여 주기 직전에 간지·오행을 '한글(한자)'로 바꾼다
        llm_ids = {id(c) for c in s["answer"].claims}
        origins = ["llm" if id(c) in llm_ids else "code" for c in claims]
        claims = [c.model_copy(update={"text": annotate(c.text)}) for c in claims]
        st = self._state()
        st.final_claims = [{"section": c.section, "text": c.text, "origin": o} for c, o in zip(claims, origins)]
        st.final_extra = list(extra)
        return {"final": render_answer(StructuredAnswer(claims=claims), extra=extra, disclaimer=guards.DISCLAIMER)}

    def _check_code_claims(self, claims: list[Claim], llm_ids: set[int]) -> list[Claim]:
        """코드가 만든 문장도 LLM 문장과 같은 근거 검사를 거친다. 위반 문장은 빼고 trace 에 남긴다.

        LLM 문장은 guard_output 에서 이미 검사·재생성했으므로 여기서는 코드가 붙인 문장만 본다.
        """
        st = self._state()
        known = set(st.contexts)
        kept, dropped = [], []
        for c in claims:
            if id(c) not in llm_ids and self._claim_violations(c, known):
                dropped.append(c.text[:40])
                continue
            kept.append(c)
        st.add_step("guard:output", f"code_claims={len(claims) - len(llm_ids & set(map(id, claims)))}",
                    f"dropped={len(dropped)}" + (f" {dropped}" if dropped else ""), type="guard", name="code_claim_check")
        return kept

    # ---- 진입점 ----
    async def run_query(self, question: str) -> dict:
        """POST /query: 상태 없는 한 번의 질의 (과정 규약)."""
        result, _ = await self.run_turn(question)
        return result

    async def run_turn(self, question: str, preset_chart: dict | None = None,
                       exclude_ids: list[str] | None = None,
                       prior_answer: str | None = None,
                       listener=None) -> tuple[dict, RequestState]:
        """한 턴을 실행하고 응답과 요청 상태를 함께 돌려준다 (/chat 이 사주 요약·보여 준 장소를 기억하는 데 쓴다)."""
        st = start_request()
        st.listener = listener   # 스트리밍 API 일 때만 진행 알림을 보낸다
        st.exclude_ids = list(exclude_ids or [])
        inputs = {"question": question}
        if preset_chart:
            inputs["preset_chart"] = preset_chart
        if prior_answer:
            inputs["prior_answer"] = prior_answer
        try:
            out = await self.graph.ainvoke(inputs, {"recursion_limit": 60})
        except (asyncio.TimeoutError, TimeoutError, ReadTimeoutError) as e:
            st.add_step("error", type(e).__name__, ERROR_MESSAGE, type="error", name="timeout")
            out = {"response": ERROR_MESSAGE}
        answer = out.get("response") or out.get("final") or ""
        contexts = [] if out.get("response") else st.context_list()
        return ({"answer": answer, "contexts": contexts, "trace": st.trace,
                 "total_latency_ms": st.total_latency_ms, "reference_date": config.REFERENCE_DATE}, st)

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
