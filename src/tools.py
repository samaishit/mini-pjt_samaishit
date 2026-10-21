# tools.py - 도메인 도구 (MCP 클라이언트 + 결정론 계산 + RAG 검색)
"""전문 에이전트가 쓰는 도구와, LLM 없이 실행되는 사주 계산 단계를 모았습니다.

- MCPHub: 사주 MCP(Node 포크)와 장소 MCP(FastMCP)에 세션을 열어 두고 재사용합니다
- compute_chart: 생년월일로 사주 요약과 12개 절기 월 달력을 계산합니다 (원본 생년월일은 여기서만 사용)
- 에이전트 도구: 사주 요약은 요청 상태(tracing.current().chart)에서 읽으므로 LLM 이 생년월일을 다룰 일이 없습니다
- 장소 선정·이사 길흉월·계획 평가는 규칙으로 계산하고, LLM 은 결과를 설명만 합니다 (data/rules 의 기준과 동일)
"""
import json
import sys
import time
from contextlib import AsyncExitStack
from datetime import date, timedelta

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src import config
from src.birth_parser import BirthInfo
from src.retriever import retrieve_docs
from src.tracing import current

STEM_ELEMENT = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土", "己": "土",
                "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
BRANCH_ELEMENT = {"寅": "木", "卯": "木", "巳": "火", "午": "火", "辰": "土", "戌": "土", "丑": "土", "未": "土",
                  "申": "金", "酉": "金", "亥": "水", "子": "水"}
CLASH = {"子": "午", "午": "子", "丑": "未", "未": "丑", "寅": "申", "申": "寅",
         "卯": "酉", "酉": "卯", "辰": "戌", "戌": "辰", "巳": "亥", "亥": "巳"}
ELEMENT_KO = {"木": "목", "火": "화", "土": "토", "金": "금", "水": "수"}
NO_CHART = "출생 정보가 없어 계산할 수 없습니다. 사용자에게 생년월일(양력/음력), 성별, 출생 시각을 요청하세요."


def _text(result) -> str:
    """MCP 도구 결과(문자열 또는 content 블록 목록)에서 텍스트만 꺼낸다."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in result)
    return str(result)


# ---------------------------------------------------------------------------
# MCP 세션 허브
# ---------------------------------------------------------------------------
class MCPHub:
    """두 MCP 서버의 세션을 앱 수명 동안 열어 둔다 (도구 호출마다 프로세스를 띄우지 않음)."""

    def __init__(self):
        self._stack = AsyncExitStack()
        self.tools: dict[str, object] = {}

    async def start(self) -> "MCPHub":
        client = MultiServerMCPClient({
            "saju": {"command": "node", "args": [str(config.PROJECT_ROOT / "mcp_servers/saju/dist/index.js")],
                     "transport": "stdio"},
            "places": {"command": sys.executable,
                       "args": [str(config.PROJECT_ROOT / "mcp_servers/places/places_server.py")],
                       "transport": "stdio"},
        })
        for server in ("saju", "places"):
            session = await self._stack.enter_async_context(client.session(server))
            for t in await load_mcp_tools(session):
                self.tools[t.name] = t
        return self

    async def close(self) -> None:
        await self._stack.aclose()

    async def call(self, name: str, args: dict, private: bool = False) -> str:
        """MCP 도구를 호출하고 trace 에 tool 단계로 남긴다.

        private=True(사주 도구)면 입력(생년월일)과 출력(원국 JSON 에 입력값이 포함됨)을 남기지 않고
        요약은 contexts 의 mcp:saju 로만 노출한다.
        """
        start = time.perf_counter()
        out = _text(await self.tools[name].ainvoke(args))
        current().add_step(f"tool:{name}",
                           "(출생 정보 생략)" if private else json.dumps(args, ensure_ascii=False),
                           "(계산 완료: 요약은 contexts 의 mcp:saju)" if private else out,
                           type="tool", name=name, latency_ms=int((time.perf_counter() - start) * 1000))
        return out


_hub: MCPHub | None = None


def set_hub(hub: MCPHub) -> None:
    global _hub
    _hub = hub


def hub() -> MCPHub:
    if _hub is None:
        raise RuntimeError("MCPHub 가 시작되지 않았습니다. agent.build_app() 을 먼저 호출하세요.")
    return _hub


# ---------------------------------------------------------------------------
# 사주 계산 (LLM 없음)
# ---------------------------------------------------------------------------
def _month_info(m: dict, yongsin_el: set[str], day_branch: str) -> dict:
    stem, branch = m["month_ganzhi"][0], m["month_ganzhi"][1]
    els = {STEM_ELEMENT[stem], BRANCH_ELEMENT[branch]}
    clash = CLASH.get(branch) == day_branch
    start, end = date.fromisoformat(m["start_date"]), date.fromisoformat(m["end_date"])
    # 이 절기 월에 들어 있는 양력 15일 → '양력 N월'로 부르는 달 (계획 월 매핑 규칙과 동일)
    fifteenth = next(start + timedelta(days=i) for i in range((end - start).days + 1)
                     if (start + timedelta(days=i)).day == 15)
    return {
        **m,
        "elements": sorted(els),
        "yongsin_match": sorted(els & yongsin_el),
        "clash_with_day_branch": clash,
        "good_month": bool(els & yongsin_el) and not clash,
        "solar_month": fifteenth.month,
        "solar_label": f"{fifteenth.year}년 {fifteenth.month}월",
    }


async def compute_chart(birth: BirthInfo, reference_date: str) -> dict:
    """사주 MCP 를 직접 호출해 요약을 만들고 contexts 에 mcp:saju 로 넣는다."""
    args = birth.to_mcp_args()
    saju = json.loads(await hub().call("calculate_saju", {**args, "now": reference_date, "format": "json"},
                                       private=True))
    cal = json.loads(await hub().call("get_luck_calendar", {**args, "from": reference_date, "months": 12},
                                      private=True))
    adv = saju["advanced"]
    yongsin = adv["yongsin"]
    yongsin_el = [STEM_ELEMENT[s] for s in yongsin]
    pillars = dict(saju["pillars"])
    if not birth.hour_known:
        pillars.pop("hour", None)
    months = [_month_info(m, set(yongsin_el), saju["dayBranch"]) for m in cal["months"]]
    chart = {
        "reference_date": reference_date,
        "hour_known": birth.hour_known,
        "calendar_input": birth.calendar + ("(윤달)" if birth.leap else ""),
        "pillars": pillars,
        "day_stem": saju["dayStem"], "day_branch": saju["dayBranch"],
        "strength": adv["dayStrength"]["strength"], "geukguk": adv["geukguk"],
        "yongsin": yongsin, "yongsin_elements": list(dict.fromkeys(yongsin_el)),
        "five_elements": saju.get("fiveElements"),
        "months": months,
        "good_move_months": [m["month_ganzhi"] for m in months if m["good_month"]],
        "avoid_move_months": [m["month_ganzhi"] for m in months if m["clash_with_day_branch"]],
    }
    current().chart = chart
    current().add_context("mcp:saju", chart_text(chart), "tool", "saju")
    return chart


def chart_text(chart: dict) -> str:
    """LLM 과 contexts 에 넣을 사주 요약 (원본 생년월일 없음)."""
    ys = ", ".join(f"{s}({ELEMENT_KO[STEM_ELEMENT[s]]}·{STEM_ELEMENT[s]})" for s in chart["yongsin"])
    pillars = " ".join(f"{k}:{v}" for k, v in chart["pillars"].items())
    lines = [
        f"[사주 요약] 기준일 {chart['reference_date']}, 입력 {chart['calendar_input']}"
        + ("" if chart["hour_known"] else ", 출생 시각 미상(시주 제외)"),
        f"원국 {pillars} / 일간 {chart['day_stem']}, 일지 {chart['day_branch']} / {chart['strength']}, {chart['geukguk']}",
        f"용신 {ys}",
        "[12개 절기 월] 번호. 양력 달(15일 기준) = 월운(세운) 기간 | 천간·지지 십성 | 12운성 | 용신 일치 | 일지 충",
    ]
    for m in chart["months"]:
        lines.append(
            f"{m['index']}. {m['solar_label']} = {m['month_ganzhi']}월({m['year_ganzhi']}년) {m['start_date']}~{m['end_date']} | "
            f"{m['stem_ten_god']}·{m['branch_ten_god']} | {m['stage12']} | "
            f"{','.join(m['yongsin_match']) or '-'} | {'충' if m['clash_with_day_branch'] else '-'}"
        )
    lines.append(f"이사 좋은 달: {', '.join(chart['good_move_months']) or '없음'} / "
                 f"이사 피할 달(일지 충): {', '.join(chart['avoid_move_months']) or '없음'}")
    return "\n".join(lines)


def find_month(month: int, year: int | None = None) -> dict | None:
    """양력 '연·월'을 그 달 15일이 속한 절기 월로 바꾼다. 12개월 범위 밖이면 None."""
    chart = current().chart
    if not chart:
        return None
    ref = date.fromisoformat(chart["reference_date"])
    years = [year] if year else [ref.year, ref.year + 1]
    for y in years:
        try:
            target = date(y, month, 15)
        except ValueError:
            return None
        for m in chart["months"]:
            if date.fromisoformat(m["start_date"]) <= target <= date.fromisoformat(m["end_date"]):
                return m
    return None


# ---------------------------------------------------------------------------
# 장소 DB 이름 찾기 (사용자가 말한 장소 → id)
# ---------------------------------------------------------------------------
def _load_places() -> list[dict]:
    rows = []
    for file, key, kind in (("regions.json", "regions", "region"), ("travel.json", "travel", "travel")):
        for p in json.loads((config.PLACES_DIR / file).read_text(encoding="utf-8"))[key]:
            rows.append({**p, "kind": kind})
    return rows


PLACES = _load_places()


def _aliases(name: str) -> set[str]:
    parts = {name}
    for token in name.replace("·", " ").split():
        if len(token) >= 2:
            parts.add(token)
            if token.endswith(("구", "시")) and len(token) >= 3:
                parts.add(token[:-1])
    return parts


def lookup_place(text: str, kind: str | None = None) -> dict | None:
    """'삿포로', '해운대', '마포구' 같은 표현을 DB 장소로 찾는다. 가장 긴 별칭이 일치하는 장소를 고른다."""
    best, best_len = None, 0
    for p in PLACES:
        if kind and p["kind"] != kind:
            continue
        for a in _aliases(p["name"]):
            if a in text and len(a) > best_len and a not in ("서울", "부산", "인천", "대구", "대전", "광주", "울산", "일본", "중국", "미국"):
                best, best_len = p, len(a)
    return best


def _place_context(p: dict) -> None:
    current().add_context(
        p["id"],
        f"{p['name']} | 방위 {p['direction']} | 오행 주 {p['elements'][0]}, 보조 {p['elements'][1]} | "
        f"{p['description']}" + (f" | 추천 월 {p['best_months']}" if p.get("best_months") else ""),
        "tool", "place")


def _brief(p: dict) -> dict:
    return {k: p[k] for k in ("id", "name", "direction", "elements", "score", "score_breakdown", "description")
            if k in p}


async def _search(tool_name: str, args: dict) -> list[dict]:
    out = await hub().call(tool_name, args)
    try:
        cands = json.loads(out)["candidates"]
    except (json.JSONDecodeError, KeyError):
        return []
    for c in cands:
        _place_context(c)
    return cands


# ---------------------------------------------------------------------------
# 에이전트 도구
# ---------------------------------------------------------------------------
def _record_retrieval(query: str, docs, doc_type: str) -> list[dict]:
    rows = []
    for d in docs:
        current().add_context(d.metadata["doc_id"], d.page_content, "rag", doc_type)
        rows.append({"doc_id": d.metadata["doc_id"], "text": d.page_content})
    current().add_step("retrieve", query, [r["doc_id"] for r in rows], type="retrieval", name="retrieve_docs")
    return rows


@tool
async def retrieve_rule_docs(query: str) -> str:
    """명리 해석 규칙 문서(오행, 십성, 용신, 월운, 충합, 방위, 여행·주거 기준, 해석 태도)를 하이브리드 검색한다.

    사주 용어나 해석 근거가 필요할 때 사용한다. 답변에서 해석을 말할 때는 이 결과의 doc_id 를 근거로 쓴다.

    Args:
        query: 검색할 내용 (예: "용신의 의미", "일지와 충하는 달")
    """
    chart = current().chart
    els = chart["yongsin_elements"] if chart else None
    docs = retrieve_docs(query, doc_type="rule", k=3, elements=els)
    return json.dumps(_record_retrieval(query, docs, "rule"), ensure_ascii=False)


@tool
async def retrieve_place_docs(query: str, kind: str = "travel") -> str:
    """장소 설명 문서(여행지 40곳, 주거 지역 30곳)를 하이브리드 검색한다.

    장소의 분위기·환경 설명을 보충할 때 사용한다. 추천 후보 선정에는 recommend_* 도구를 쓴다.

    Args:
        query: 검색할 내용 (예: "바다가 가까운 조용한 곳")
        kind: "travel"(여행지) 또는 "region"(주거 지역)
    """
    docs = [d for d in retrieve_docs(query, doc_type="place", k=6) if d.metadata.get("place_kind") == kind][:3]
    return json.dumps(_record_retrieval(query, docs, "place"), ensure_ascii=False)


@tool
async def element_direction_map() -> str:
    """오행과 방위·계절·환경의 매핑표를 조회한다. 추천 이유를 오행·방위로 설명할 때 사용한다."""
    out = await hub().call("element_direction_map", {})
    current().add_context("mcp:element_direction_map", out, "tool", "rule")
    return out


@tool
async def recommend_travel(month_indices: list[int], scope: str = "both") -> str:
    """사주 요약의 절기 월 번호(1~12)별로 용신에 맞는 여행지를 추천한다 (장소 DB 점수순, 같은 장소 반복 없음).

    월별 여행 추천에는 반드시 이 도구를 쓴다. 12개월 전체면 month_indices=[1,...,12].

    Args:
        month_indices: 절기 월 번호 목록 (사주 요약의 '12개 절기 월' 번호)
        scope: "both"(국내 1 + 해외 1), "domestic"(국내만), "overseas"(해외만)
    """
    chart = current().chart
    if not chart:
        return NO_CHART
    scopes = ["domestic", "overseas"] if scope == "both" else [scope]
    used: list[str] = []
    result = []
    for idx in month_indices:
        if not 1 <= idx <= 12:
            continue
        m = chart["months"][idx - 1]
        picks = {}
        for sc in scopes:
            cands = await _search("search_travel", {"elements": chart["yongsin"], "scope": sc,
                                                     "month": m["solar_month"], "limit": 3, "exclude_ids": used})
            if cands:
                used.append(cands[0]["id"])
                picks[sc] = {"pick": _brief(cands[0]), "alternatives": [_brief(c) for c in cands[1:]]}
        result.append({"index": idx, "month": f"{m['month_ganzhi']}월", "period": f"{m['start_date']}~{m['end_date']}",
                       "ten_gods": f"{m['stem_ten_god']}·{m['branch_ten_god']}", "good_month": m["good_month"],
                       "clash": m["clash_with_day_branch"], **picks})
    return json.dumps(result, ensure_ascii=False)


@tool
async def get_travel(travel_id: str) -> str:
    """여행지 id 로 상세 정보를 조회한다 (예: T-OS-SAPPORO)."""
    out = await hub().call("get_travel", {"travel_id": travel_id})
    try:
        _place_context(json.loads(out))
    except json.JSONDecodeError:
        pass
    return out


@tool
async def recommend_residence(sido: str | None = None) -> str:
    """용신에 맞는 주거 지역 Top 3 와 이사하기 좋은 달·피할 달을 돌려준다 (장소 DB 점수순).

    주거·이사 추천에는 반드시 이 도구를 쓴다. 시세·매물·투자 정보는 없다.

    Args:
        sido: 특정 시·도로 제한할 때 (예: "서울특별시"). 없으면 전체
    """
    chart = current().chart
    if not chart:
        return NO_CHART
    args = {"elements": chart["yongsin"], "limit": 3}
    if sido:
        args["sido"] = sido
    cands = await _search("search_regions", args)
    months = {m["month_ganzhi"]: f"{m['month_ganzhi']}월 {m['start_date']}~{m['end_date']}" for m in chart["months"]}
    return json.dumps({
        "top3": [_brief(c) for c in cands],
        "good_move_months": [months[g] for g in chart["good_move_months"]],
        "avoid_move_months": [months[a] + " (일지 충)" for a in chart["avoid_move_months"]],
        "basis": "rule:residence:selection, rule:residence:move-timing",
    }, ensure_ascii=False)


@tool
async def get_region(region_id: str) -> str:
    """주거 지역 id 로 상세 정보를 조회한다 (예: R-SEOUL-MAPO)."""
    out = await hub().call("get_region", {"region_id": region_id})
    try:
        _place_context(json.loads(out))
    except json.JSONDecodeError:
        pass
    return out


@tool
async def evaluate_plan(month: int, kind: str, place: str | None = None, year: int | None = None) -> str:
    """사용자의 계획 하나를 좋음/보통/주의로 평가한다 (rule:travel:plan-rating 기준).

    계획마다 한 번씩 호출한다. 예: "12월 삿포로 여행" → month=12, kind="travel", place="삿포로"
    "내년 3월 이사" → month=3, year=2027, kind="move"

    Args:
        month: 계획한 양력 월 (1~12)
        kind: "travel"(여행) 또는 "move"(이사)
        place: 사용자가 말한 장소 이름 (없으면 생략)
        year: 계획한 연도 (사용자가 말했거나 '내년'이면 기준일 다음 해). 없으면 기준일 이후 가장 가까운 해
    """
    chart = current().chart
    if not chart:
        return NO_CHART
    m = find_month(month, year)
    if m is None:
        res = {"rating": None, "status": "out_of_window",
               "message": f"조회 범위({chart['months'][0]['start_date']}~{chart['months'][-1]['end_date']}) 밖이라 평가하지 않습니다.",
               "good_move_months": chart["good_move_months"] if kind == "move" else None}
        current().add_context("tool:evaluate_plan", json.dumps(res, ensure_ascii=False), "tool", "plan")
        return json.dumps(res, ensure_ascii=False)

    target = lookup_place(place, "travel" if kind == "travel" else "region") if place else None
    score = None
    alternatives = []
    if kind == "travel":
        cands = await _search("search_travel", {"elements": chart["yongsin"], "scope": "all",
                                                 "month": m["solar_month"], "limit": 40})
        if target:
            score = next((c["score"] for c in cands if c["id"] == target["id"]), None)
        alternatives = [_brief(c) for c in cands if not target or c["id"] != target["id"]][:3]
    else:
        # 이사 계획: 지역을 말했으면 그 지역 점수를, 아니면 용신에 맞는 추천 지역을 함께 돌려준다
        cands = await _search("search_regions", {"elements": chart["yongsin"], "limit": 30 if target else 3})
        if target:
            score = next((c["score"] for c in cands if c["id"] == target["id"]), None)
        alternatives = [_brief(c) for c in cands if not target or c["id"] != target["id"]][:3]

    month_good = bool(m["yongsin_match"])
    if m["clash_with_day_branch"] or (score is not None and score <= 1):
        rating = "주의"
    elif month_good and (score is None or score >= 3):   # 장소가 없거나 DB 외 장소면 월운만으로 평가
        rating = "좋음"
    else:
        rating = "보통"
    res = {
        "rating": rating,
        "month": f"{m['month_ganzhi']}월 {m['start_date']}~{m['end_date']}",
        "month_yongsin_match": m["yongsin_match"], "clash_with_day_branch": m["clash_with_day_branch"],
        "place": target["name"] if target else place, "place_id": target["id"] if target else None,
        "in_db": bool(target) if place else None, "place_score": score,
        "alternatives": alternatives if rating == "주의" or (place and not target) else [],
        "suggested_regions": alternatives if kind == "move" and not target else [],
        "better_move_months": chart["good_move_months"] if kind == "move" and rating == "주의" else [],
        "basis": "rule:travel:plan-rating" if kind == "travel" else "rule:residence:move-timing",
    }
    current().add_context("tool:evaluate_plan", json.dumps(res, ensure_ascii=False), "tool", "plan")
    return json.dumps(res, ensure_ascii=False)


SAJU_TOOLS = [retrieve_rule_docs, element_direction_map]
TRAVEL_TOOLS = [recommend_travel, get_travel, retrieve_place_docs]
RESIDENCE_TOOLS = [recommend_residence, get_region, retrieve_place_docs]
PLAN_TOOLS = [evaluate_plan, recommend_travel]
