# chat.py - 웹 화면 '대화' 탭용 단기 기억 (POST /chat). POST /query 는 그대로 상태 없음
"""세션(브라우저 대화)마다 짧은 기억을 서버 메모리에 둔다.

기억하는 것 (원본 생년월일은 저장하지 않는다):
  - 사주 요약(chart): 첫 턴에서 계산한 간지·용신 등 파생값. 다음 턴에 생년월일 없이 다시 쓴다
  - 턴 기록: <BIRTH> 로 가린 질문과 답변 첫 줄
  - 마지막 질문(가린 문장): "그럼 해외는?" 같은 이어 묻기의 맥락
  - 이미 보여 준 장소 id: "다른 곳도" 요청이면 빼고 추천한다

이어 묻기 판단은 규칙(LLM 없음)이다: '그럼/그러면/다른…'으로 시작하거나, 짧고(40자 이하) 의도 키워드가 없는 말.

답변 필터 (대화 탭 화면용, 근거 검증이 끝난 최종 문장에만 적용):
  1) 중복: 이 대화에서 이미 보여 준 문장(섹션+문장 키)은 접는다. 코드가 만든 문장(사주 요약·용어 풀이·여행 표 등)은
     같은 입력이면 같은 문장이라 키로 정확히 걸러진다. '한눈에 보기'는 늘 보여 준다
  2) 범위: 이어 묻기면 이번에 한 말의 의도(없으면 라우터가 고른 에이전트)에 맞는 섹션만 펼치고 나머지는 접는다
  - '다시/전체/정리' 같은 말이면 필터를 끄고 전체를 보여 준다
  - 접은 문장도 응답의 view 에 담아 화면에서 펼쳐 볼 수 있다 (정보를 지우지 않음)
  - 직전에 보여 준 답변 요약을 다음 턴 작성 LLM 에 넘겨 같은 해석을 되풀이하지 않게 한다
세션은 마지막 사용 후 SESSION_TTL_S 가 지나면 지우고, 최대 MAX_SESSIONS 개만 둔다.
"""
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field

from src import guards
from src.agent import FULL_YEAR_RE, PLAN_EVAL_RE, RESIDENCE_RE, SAJU_RE, TRAVEL_RE, required_agents
from src.birth_parser import parse
from src.schemas import Claim, StructuredAnswer, render_answer

SESSION_TTL_S = 60 * 60
MAX_SESSIONS = 200
MAX_TURNS = 10
FOLLOWUP_LEAD_RE = re.compile(r"^\s*(그럼|그러면|그리고|그건|그거|그때|그 달|거기|거긴|다른|또|추가로|혹시|그중|그 중|아까)")
EXCLUDE_RE = re.compile(r"다른|말고|또 다른|새로운|빼고")
# 인사·감사 같은 말은 이어 묻기로 보지 않는다 (앞 질문을 다시 돌리지 않음)
SMALLTALK_RE = re.compile(r"^\s*(고마워요?|고맙습니다|감사(합니다|해요)?|안녕(하세요)?|ㅎ+|ㅋ+|좋아요?|알겠(어|어요|습니다)|"
                          r"오케이|ok|네|응)[\s.!~?]*$", re.IGNORECASE)
FOLLOWUP_MAX_CHARS = 40


@dataclass
class Turn:
    question: str          # <BIRTH> 로 가린 이번 질문
    answer_head: str       # 답변 첫 줄 (화면의 대화 목록·맥락용)
    kind: str              # answer | refusal | clarification


@dataclass
class Session:
    id: str
    updated: float = field(default_factory=time.monotonic)
    chart: dict | None = None
    turns: list[Turn] = field(default_factory=list)
    last_question: str | None = None    # 이어 묻기 맥락 (가린 문장)
    shown_place_ids: list[str] = field(default_factory=list)
    shown_keys: list[str] = field(default_factory=list)   # 화면에 이미 보여 준 문장 키 (중복 필터)
    last_answer_digest: str | None = None                  # 직전에 보여 준 답변 요약 (작성 LLM 에 넘김)

    def brief(self) -> dict:
        """화면의 '기억된 정보' 패널용 요약 (원본 생년월일 없음)."""
        ch = self.chart
        return {
            "session_id": self.id,
            "turns": len(self.turns),
            "has_chart": bool(ch),
            "chart_brief": (f"일간 {ch['day_stem']}, 용신 {'·'.join(ch['yongsin'])}({'·'.join(ch['yongsin_elements'])})"
                            if ch else None),
            "last_question": self.last_question,
            "shown_places": len(self.shown_place_ids),
        }


class ChatMemory:
    def __init__(self, ttl_s: float = SESSION_TTL_S, max_sessions: int = MAX_SESSIONS):
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self.sessions: dict[str, Session] = {}

    def _evict(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self.sessions.items() if now - s.updated > self.ttl_s]:
            del self.sessions[sid]
        while len(self.sessions) > self.max_sessions:   # 가장 오래 안 쓴 세션부터
            del self.sessions[min(self.sessions, key=lambda k: self.sessions[k].updated)]

    def get(self, session_id: str | None) -> Session:
        self._evict()
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        s = Session(id=uuid.uuid4().hex)
        self.sessions[s.id] = s
        self._evict()
        return s

    def drop(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None


def is_followup(masked_message: str, session: Session) -> bool:
    """이전 질문에 이어 묻는 말인가 (규칙)."""
    if not session.last_question or SMALLTALK_RE.search(masked_message):
        return False
    if FOLLOWUP_LEAD_RE.search(masked_message):
        return True
    has_intent = any(rx.search(masked_message) for rx in (PLAN_EVAL_RE, TRAVEL_RE, RESIDENCE_RE, SAJU_RE, FULL_YEAR_RE))
    return not has_intent and len(masked_message.strip()) <= FOLLOWUP_MAX_CHARS


MARK = "(이어서 묻는 말)"
KEEP_FOLLOWUPS = 3   # 맥락에는 첫 질문과 최근 이어 묻기 3개(이번 말 포함)까지만 남긴다


def compose_followup(previous_masked: str, message: str) -> str:
    """이어 묻기를 이전 질문(가린 문장)과 합쳐 한 번의 질의로 만든다."""
    first, *rest = previous_masked.split(f"\n{MARK} ")
    kept = rest[-(KEEP_FOLLOWUPS - 1):] if KEEP_FOLLOWUPS > 1 else []
    return "\n".join([first] + [f"{MARK} {r}" for r in kept] + [f"{MARK} {message.strip()}"])


FULL_VIEW_RE = re.compile(r"다시|전체|처음부터|모두 보여|정리해")
SECTIONS_BY_AGENT = {"travel_agent": {"여행 추천"}, "residence_agent": {"주거 추천", "이사 시기"},
                     "plan_agent": {"계획 평가"}, "saju_agent": {"사주 해석", "월별 운세"}}
ALWAYS_SECTIONS = {"한눈에 보기", "안내", "용어 풀이", "근거 자료"}   # 범위 필터와 상관없이 펼친다 (중복이면 접힘)
NEVER_FOLD = {"한눈에 보기"}                                           # 이번 결론은 늘 보여 준다
DIGEST_SECTIONS = ("한눈에 보기", "사주 해석", "계획 평가", "여행 추천", "주거 추천", "이사 시기")
DIGEST_MAX_CHARS = 600


def claim_key(section: str, text: str) -> str:
    return hashlib.sha1(f"{section}|{text}".encode("utf-8")).hexdigest()[:16]


def sections_for(agents: list[str]) -> set[str]:
    return set().union(*(SECTIONS_BY_AGENT.get(a, set()) for a in agents)) if agents else set()


def asked_sections(latest_masked: str) -> set[str]:
    """이번 말이 직접 물은 섹션 (의도 키워드가 있을 때만). 이 섹션은 앞에서 봤더라도 다시 보여 준다."""
    explicit = any(rx.search(latest_masked) for rx in (PLAN_EVAL_RE, TRAVEL_RE, RESIDENCE_RE, SAJU_RE, FULL_YEAR_RE))
    return sections_for(required_agents(latest_masked)) if explicit else set()


def focus_sections(latest_masked: str, route: list[str]) -> set[str]:
    """이번 말이 원하는 섹션. 이번 말에 의도 키워드가 있으면 그것을, 없으면 라우터가 고른 에이전트를 따른다."""
    return (asked_sections(latest_masked) or sections_for(route)) | ALWAYS_SECTIONS


def build_view(claims: list[dict], extra: list[str], session: "Session", latest_masked: str, route: list[str],
               followup: bool, full: bool) -> tuple[str, dict]:
    """최종 문장을 중복·범위 기준으로 펼침/접힘으로 나누고, 펼친 문장만으로 답변 마크다운을 만든다."""
    shown_before = set(session.shown_keys)
    first = not shown_before
    # 대화의 두 번째 턴부터는 (이어 묻기든 새 질문이든) 이번 말의 범위로 거른다
    focus = focus_sections(latest_masked, route) if not first and not full else None
    asked = asked_sections(latest_masked)
    items = []
    for c in claims:
        key = claim_key(c["section"], c["text"])
        repeated = not full and key in shown_before and c["section"] not in NEVER_FOLD
        in_focus = focus is None or c["section"] in focus
        # 이번에 직접 물은 섹션은 앞에서 봤더라도 보여 준다
        show = in_focus and (not repeated or c["section"] in asked)
        items.append({**c, "key": key, "repeated": repeated, "in_focus": in_focus, "shown": show})
    # 이번 범위의 내용이 모두 앞에서 본 것이면, 빈 답이 되지 않게 범위 안 문장은 다시 보여 준다
    if focus is not None and not any(i["shown"] and i["section"] not in ALWAYS_SECTIONS for i in items):
        for i in items:
            if i["in_focus"] and i["section"] not in ALWAYS_SECTIONS:
                i["shown"] = True
    shown = [i for i in items if i["shown"]]
    hidden_repeated = sum(1 for i in items if not i["shown"] and i["repeated"])
    hidden_scope = sum(1 for i in items if not i["shown"] and not i["repeated"])
    answer = render_answer(StructuredAnswer(claims=[Claim(section=i["section"], text=i["text"], doc_ids=[]) for i in shown]),
                           extra=extra, disclaimer=guards.DISCLAIMER)
    if hidden_repeated or hidden_scope:
        parts = ([f"앞 답변과 같은 내용 {hidden_repeated}건"] if hidden_repeated else []) + \
                ([f"이번 질문과 다른 섹션 {hidden_scope}건"] if hidden_scope else [])
        answer += f"\n\n_{', '.join(parts)}은 접어 두었습니다. 아래에서 펼쳐 볼 수 있습니다._"
    sections = []
    for i in items:
        if not sections or sections[-1]["name"] != i["section"]:
            sections.append({"name": i["section"], "claims": []})
        sections[-1]["claims"].append({k: i[k] for k in ("text", "origin", "repeated", "in_focus", "shown")})
    mode = "full" if full or first else "focus"
    view = {"mode": mode, "focus": sorted(focus) if focus else None, "hidden_repeated": hidden_repeated,
            "hidden_out_of_focus": hidden_scope, "sections": sections}
    return answer, view


def answer_digest(items: list[dict]) -> str:
    """다음 턴 작성 LLM 에 넘길 직전 답변 요약 (펼쳐 보여 준 핵심 섹션만, 생년월일 형태는 다시 가림)."""
    lines = [f"- [{i['section']}] {i['text']}" for i in items if i["shown"] and i["section"] in DIGEST_SECTIONS]
    return parse("\n".join(lines)).masked_text[:DIGEST_MAX_CHARS]


def turn_kind(result: dict) -> str:
    for t in result.get("trace", []):
        if t["step"] == "guard:input" and str(t["output"]).startswith("block:"):
            return "refusal"
    parse_step = next((t for t in result.get("trace", []) if t["step"] == "parse:birth"), None)
    if parse_step and "clarification=None" not in str(parse_step["output"]):
        return "clarification"
    return "answer"


def shown_places(st) -> list[str]:
    """이번 턴에서 답변에 나온 장소 id (여행 표·주거 Top 3·계획 대안)."""
    ids = []
    for row in st.travel_picks.values():
        for scope in ("domestic", "overseas"):
            pick = (row.get(scope) or {}).get("pick")
            if pick:
                ids.append(pick["id"])
    if st.residence:
        ids += [r["id"] for r in st.residence["top3"]]
    for r in st.plan_results:
        ids += [a["id"] for a in r.get("alternatives") or []]
    return ids


class ChatService:
    """POST /chat: 세션 기억을 붙여 SajuPlannerApp.run_turn 을 부른다."""

    def __init__(self, app, memory: ChatMemory | None = None):
        self.app = app
        self.memory = memory or ChatMemory()

    async def turn(self, session_id: str | None, message: str, listener=None) -> dict:
        session = self.memory.get(session_id)
        parsed = parse(message)
        follow = parsed.birth is None and is_followup(parsed.masked_text, session)
        question = compose_followup(session.last_question, message) if follow else message
        preset = session.chart if parsed.birth is None else None   # 새 생년월일이 오면 새로 계산
        exclude = session.shown_place_ids if follow and EXCLUDE_RE.search(parsed.masked_text) else []
        prior = session.last_answer_digest if follow else None
        kwargs = {"listener": listener} if listener else {}
        result, st = await self.app.run_turn(question, preset_chart=preset, exclude_ids=exclude, prior_answer=prior,
                                             **kwargs)

        kind = turn_kind(result)
        if st.chart:
            session.chart = st.chart
        if kind == "answer":
            # 이어 묻기의 맥락은 가린 문장만 남긴다 (원본 생년월일은 parse 가 <BIRTH> 로 바꾼 뒤 저장)
            session.last_question = parse(question).masked_text
            session.shown_place_ids = list(dict.fromkeys(session.shown_place_ids + shown_places(st)))
        view = None
        if kind == "answer" and getattr(st, "final_claims", None):
            full = bool(FULL_VIEW_RE.search(parsed.masked_text))
            answer, view = build_view(st.final_claims, st.final_extra, session, parsed.masked_text,
                                      getattr(st, "route", []), follow, full)
            items = [{**c, "section": sec["name"]} for sec in view["sections"] for c in sec["claims"]]
            session.shown_keys = list(dict.fromkeys(
                session.shown_keys + [claim_key(i["section"], i["text"]) for i in items if i["shown"]]))[-500:]
            session.last_answer_digest = answer_digest(items) or session.last_answer_digest
            result = {**result, "answer_full": result["answer"], "answer": answer, "view": view}
        head = next((ln.lstrip("- ").strip() for ln in result["answer"].splitlines()
                     if ln.strip() and not ln.startswith("#")), "")
        session.turns = (session.turns + [Turn(parsed.masked_text, head[:200], kind)])[-MAX_TURNS:]
        session.updated = time.monotonic()
        return {**result, "session": {**session.brief(), "followup": follow, "kind": kind}}
