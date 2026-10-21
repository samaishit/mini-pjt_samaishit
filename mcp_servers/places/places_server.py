# places_server.py - 사주 라이프 플래너 장소 DB MCP 서버 (fastmcp)
"""노출하는 도구 5개:
  - element_direction_map()                          오행 ↔ 방위·계절·환경 매핑
  - search_travel(elements, scope, month, ...)       용신 기준 여행지 후보 (점수순)
  - get_travel(travel_id)                            여행지 상세
  - search_regions(elements, ...)                    용신 기준 주거 지역 후보 (점수순)
  - get_region(region_id)                            주거 지역 상세

실행: python places_server.py
(stdio 모드로 대기합니다. 클라이언트가 자식 프로세스로 띄워서 사용합니다)

점수 규칙(결정론): 주 오행이 용신이면 +2, 보조 오행이 용신이면 +1,
방위가 용신 오행의 방위와 겹치면 +1, 여행지는 추천 월이면 +1.
동점이면 id 순으로 정렬합니다. LLM은 이 후보 안에서만 고르고 이유를 설명합니다.
"""
import json
import os

from fastmcp import FastMCP

mcp = FastMCP("saju-places")

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "..", "..", "data", "places")
REGIONS = json.load(open(os.path.join(DATA_DIR, "regions.json"), encoding="utf-8"))["regions"]
TRAVEL = json.load(open(os.path.join(DATA_DIR, "travel.json"), encoding="utf-8"))["travel"]
REGION_BY_ID = {r["id"]: r for r in REGIONS}
TRAVEL_BY_ID = {t["id"]: t for t in TRAVEL}

# 오행 정의는 이 표 한 곳에서만 관리합니다
ELEMENT_INFO = {
    "木": {"name": "목", "direction": "동", "season": "봄", "environment": "숲, 정원, 녹지, 산림"},
    "火": {"name": "화", "direction": "남", "season": "여름", "environment": "활기 있는 도심, 문화, 햇볕, 화산"},
    "土": {"name": "토", "direction": "중앙", "season": "환절기", "environment": "평지, 고도·유적, 안정된 주거지"},
    "金": {"name": "금", "direction": "서", "season": "가을", "environment": "바위·산악, 산업, 금융, 공항"},
    "水": {"name": "수", "direction": "북", "season": "겨울", "environment": "바다, 강, 호수, 눈"},
}
# 천간과 한글 오행 이름도 받아 줍니다 (예: 丙 → 火, 화 → 火)
STEM_TO_ELEMENT = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
}
KOREAN_TO_ELEMENT = {"목": "木", "화": "火", "토": "土", "금": "金", "수": "水"}
# 8방위가 품는 오행 (예: 남동 = 남(火) + 동(木))
DIRECTION_ELEMENTS = {
    "동": {"木"}, "서": {"金"}, "남": {"火"}, "북": {"水"}, "중앙": {"土"},
    "북동": {"水", "木"}, "남동": {"火", "木"}, "남서": {"火", "金"}, "북서": {"水", "金"},
}


def normalize_elements(elements: list[str]) -> list[str]:
    """천간·한글 이름·한자 오행이 섞인 목록을 한자 오행 목록으로 바꾼다 (순서 유지, 중복 제거)."""
    out = []
    for e in elements:
        e = e.strip()
        el = STEM_TO_ELEMENT.get(e) or KOREAN_TO_ELEMENT.get(e) or (e if e in ELEMENT_INFO else None)
        if el and el not in out:
            out.append(el)
    return out


def score_place(place: dict, yongsin: list[str], month: int | None = None) -> dict:
    """장소 하나의 점수와 근거를 계산한다."""
    primary, secondary = place["elements"][0], place["elements"][1]
    breakdown = {
        "primary_element": 2 if primary in yongsin else 0,
        "secondary_element": 1 if secondary in yongsin else 0,
        "direction": 1 if DIRECTION_ELEMENTS.get(place["direction"], set()) & set(yongsin) else 0,
    }
    if month is not None:
        breakdown["best_month"] = 1 if month in place.get("best_months", []) else 0
    return {"score": sum(breakdown.values()), "score_breakdown": breakdown}


def rank(places: list[dict], yongsin: list[str], limit: int, month: int | None = None) -> list[dict]:
    scored = []
    for p in places:
        s = score_place(p, yongsin, month)
        scored.append({**p, **s})
    scored.sort(key=lambda x: (-x["score"], x["id"]))
    return scored[:limit]


@mcp.tool()
def element_direction_map() -> str:
    """오행(木火土金水)별 방위, 계절, 어울리는 환경 매핑표와 8방위가 품는 오행을 돌려준다.

    추천 이유를 설명할 때 오행과 방위·환경의 연결 근거가 필요하면 사용한다.
    장소 후보를 찾을 때는 사용하지 않는다 (search_travel, search_regions 사용).
    """
    return json.dumps(
        {"elements": ELEMENT_INFO, "direction_elements": {k: sorted(v) for k, v in DIRECTION_ELEMENTS.items()}},
        ensure_ascii=False,
    )


@mcp.tool()
def search_travel(elements: list[str], scope: str = "all", month: int | None = None,
                  limit: int = 5, exclude_ids: list[str] | None = None) -> str:
    """용신 오행에 맞는 여행지 후보를 점수순으로 조회한다.

    월별 여행지 추천, 사용자가 말한 여행 계획의 대안 찾기에 사용한다.
    주거 지역에는 사용하지 않는다 (search_regions 사용).

    Args:
        elements: 용신 오행 또는 천간 목록 (예: ["丙", "癸", "甲"] 또는 ["火", "水", "木"])
        scope: "domestic"(국내), "overseas"(해외), "all"(전체)
        month: 여행할 양력 월 1~12 (추천 월 가산점에 사용, 없으면 생략)
        limit: 돌려줄 후보 수 (기본 5)
        exclude_ids: 이미 추천해서 제외할 여행지 id 목록
    """
    yongsin = normalize_elements(elements)
    if not yongsin:
        return "에러: elements에서 오행을 찾지 못했습니다. 천간(甲~癸)이나 오행(木火土金水, 목화토금수)으로 다시 호출하세요."
    if scope not in ("domestic", "overseas", "all"):
        return "에러: scope는 domestic, overseas, all 중 하나여야 합니다."
    if month is not None and not 1 <= month <= 12:
        return "에러: month는 1~12 사이여야 합니다."
    pool = [t for t in TRAVEL
            if (scope == "all" or t["scope"] == scope) and t["id"] not in set(exclude_ids or [])]
    picks = rank(pool, yongsin, limit, month)
    return json.dumps({"yongsin_elements": yongsin, "scope": scope, "month": month, "candidates": picks},
                      ensure_ascii=False)


@mcp.tool()
def get_travel(travel_id: str) -> str:
    """여행지 id로 상세 정보를 조회한다.

    사용자가 말한 특정 여행지가 DB에 있는지 확인하거나 상세 설명이 필요할 때 사용한다.

    Args:
        travel_id: 여행지 id (예: "T-OS-SAPPORO")
    """
    t = TRAVEL_BY_ID.get(travel_id)
    if t:
        return json.dumps(t, ensure_ascii=False)
    names = ", ".join(f"{x['id']}({x['name']})" for x in TRAVEL)
    return f"'{travel_id}' 여행지는 DB에 없습니다. 등록된 여행지: {names}"


@mcp.tool()
def search_regions(elements: list[str], limit: int = 5, sido: str | None = None) -> str:
    """용신 오행에 맞는 주거 지역(특별시·광역시의 구) 후보를 점수순으로 조회한다.

    이사·주거 지역 추천에 사용한다. 시세, 매물, 단지 정보는 DB에 없으며 제공하지 않는다.
    여행지에는 사용하지 않는다 (search_travel 사용).

    Args:
        elements: 용신 오행 또는 천간 목록 (예: ["戊", "丙", "甲"])
        limit: 돌려줄 후보 수 (기본 5)
        sido: 특정 시·도로 제한할 때 (예: "서울특별시", "부산광역시")
    """
    yongsin = normalize_elements(elements)
    if not yongsin:
        return "에러: elements에서 오행을 찾지 못했습니다. 천간(甲~癸)이나 오행(木火土金水, 목화토금수)으로 다시 호출하세요."
    pool = [r for r in REGIONS if sido is None or r["sido"] == sido]
    if not pool:
        sidos = sorted({r["sido"] for r in REGIONS})
        return f"에러: '{sido}' 지역은 DB에 없습니다. 가능한 값: {', '.join(sidos)}"
    picks = rank(pool, yongsin, limit)
    return json.dumps({"yongsin_elements": yongsin, "sido": sido, "candidates": picks}, ensure_ascii=False)


@mcp.tool()
def get_region(region_id: str) -> str:
    """주거 지역 id로 상세 정보를 조회한다.

    사용자가 말한 지역이 DB에 있는지 확인하거나 상세 설명이 필요할 때 사용한다.

    Args:
        region_id: 지역 id (예: "R-SEOUL-MAPO")
    """
    r = REGION_BY_ID.get(region_id)
    if r:
        return json.dumps(r, ensure_ascii=False)
    names = ", ".join(f"{x['id']}({x['name']})" for x in REGIONS)
    return f"'{region_id}' 지역은 DB에 없습니다. 등록된 지역: {names}"


if __name__ == "__main__":
    # stdio 클라이언트가 도구를 부를 때마다 서버를 띄우므로 시작 배너는 끈다
    mcp.run(show_banner=False)
