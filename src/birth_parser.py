# birth_parser.py - 결정론 출생 정보 파서 (LLM 미사용)
"""질문 문장에서 생년월일, 양/음력(윤달), 성별, 출생 시각을 규칙으로 뽑고
원문 날짜·시각을 <BIRTH>로 바꾼 문장을 돌려줍니다.

- 생년월일로 보는 날짜: 같은 구절에 생/출생/태어난/생년월일/생일이 있거나,
  연도가 기준일보다 10년 이상 앞선 4자리 연도 날짜 (여행·이사 계획 날짜와 구분)
- 애매하면 LLM을 부르지 않고 되묻습니다 (CLARIFY_MESSAGES 고정 문구)
- 출생 시각이 없으면 시주를 제외하고(hour_known=False), 음력에 '윤'이 없으면 평달로 봅니다
- 제3자 생년월일 차단은 이 파서보다 먼저 실행되는 입력 가드가 맡습니다
"""
import re
from dataclasses import dataclass, field
from datetime import date

from src import config

PLACEHOLDER = "<BIRTH>"

CLARIFY_MESSAGES = {
    "two_digit_year": "출생 연도를 네 자리로 알려 주세요. (예: 1992년 4월 12일)",
    "calendar_unknown": "생년월일이 양력인지 음력인지 알려 주세요. 음력 윤달이면 '윤'을 함께 적어 주세요.",
    "invalid_date": "존재하지 않는 날짜예요. 생년월일을 다시 확인해 알려 주세요.",
    "gender_missing": "정확한 계산을 위해 성별(남/여)을 함께 알려 주세요.",
    "ambiguous_hour": "출생 시각이 오전인지 오후인지 알려 주세요. (예: 오후 2시 30분, 14:30) 모르시면 '시간 모름'이라고 적어 주세요.",
    "multiple_birth_dates": "생년월일로 보이는 날짜가 여러 개예요. 본인의 생년월일 하나만 알려 주세요.",
    "future_date": "생년월일이 기준일보다 미래예요. 생년월일을 다시 확인해 알려 주세요.",
}

# '생'은 '생각·학생'과 구분하려고 날짜·시각 바로 뒤에 붙은 경우만 본다 (15일생, 92년생, 양력생, 9시생)
BIRTH_KEYWORD = re.compile(r"생년월일|태어|출생|생일|(?:일|년|시|분|반|력)\s*생")
SEPARATORS = re.compile(r"[.!?\n,]")

# 4자리 연도 날짜: 1992년 4월 12일 / 1993년 윤3월 10일 / 1992.4.12 / 1992-04-12 / 1992/04/12
DATE_PATTERNS = [
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})\s*년\s*(?P<leap>윤\s*)?(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일"),
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})\s*[.\-/]\s*(?P<m>\d{1,2})\s*[.\-/]\s*(?P<d>\d{1,2})(?!\d)\.?"),
]
# 2자리 연도: 92년생 / 92년 4월 12일 / 92.4.12
TWO_DIGIT_PATTERNS = [
    re.compile(r"(?<!\d)\d{2}\s*년\s*생"),
    re.compile(r"(?<!\d)\d{2}\s*년\s*(?:윤\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일"),
    re.compile(r"(?<!\d)\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}(?!\d)"),
]
AMPM_TIME = re.compile(r"(?P<ampm>오전|오후|새벽|아침|낮|저녁|밤)\s*(?P<h>\d{1,2})\s*시(?:\s*(?P<min>\d{1,2})\s*분|\s*(?P<half>반))?")
COLON_TIME = re.compile(r"(?<!\d)(?P<h>\d{1,2}):(?P<min>\d{2})(?!\d)")
BARE_TIME = re.compile(r"(?<!\d)(?P<h>\d{1,2})\s*시(?:\s*(?P<min>\d{1,2})\s*분|\s*(?P<half>반))?")
NAMED_TIME = re.compile(r"(?P<name>정오|자정)")
UNKNOWN_TIME = re.compile(r"(시간|시각|태어난\s*시)\S{0,2}\s*(모름|몰라|모르|미상)")
GENDER_PATTERNS = {
    "남": re.compile(r"(?:남성|남자)(?!친구|아이|애)|(?<![가-힣])남(?![가-힣])"),
    "여": re.compile(r"(?:여성|여자)(?!친구|아이|애)|(?<![가-힣])여(?![가-힣])"),
}


@dataclass
class BirthInfo:
    year: int
    month: int
    day: int
    calendar: str          # "solar" | "lunar"
    leap: bool
    gender: str            # "남" | "여"
    hour: int | None = None
    minute: int | None = None

    @property
    def hour_known(self) -> bool:
        return self.hour is not None

    def to_mcp_args(self) -> dict:
        """사주 MCP(calculate_saju, get_luck_calendar)에 넘길 인자. 시각을 모르면 12:00으로 계산한다."""
        return {
            "year": self.year, "month": self.month, "day": self.day,
            "hour": self.hour if self.hour is not None else 12,
            "minute": self.minute if self.minute is not None else 0,
            "gender": self.gender, "calendar": self.calendar, "leap": self.leap,
        }


@dataclass
class ParseResult:
    birth: BirthInfo | None
    masked_text: str
    clarification: str | None = None          # CLARIFY_MESSAGES 의 키
    notes: list[str] = field(default_factory=list)

    @property
    def clarification_message(self) -> str | None:
        return CLARIFY_MESSAGES.get(self.clarification) if self.clarification else None


@dataclass
class _DateHit:
    start: int
    end: int
    year: int
    month: int
    day: int
    leap: bool


def _find_dates(text: str) -> list[_DateHit]:
    hits: list[_DateHit] = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            if any(h.start <= m.start() < h.end for h in hits):
                continue
            leap = bool(m.groupdict().get("leap"))
            hits.append(_DateHit(m.start(), m.end(), int(m["y"]), int(m["m"]), int(m["d"]), leap))
    return sorted(hits, key=lambda h: h.start)


def _clauses(text: str, hits: list[_DateHit]) -> list[tuple[int, int]]:
    """날짜 안의 점(.)을 구분자로 오해하지 않도록 날짜 구간을 건너뛰며 구절 경계를 만든다."""
    bounds, start = [], 0
    for m in SEPARATORS.finditer(text):
        if any(h.start <= m.start() < h.end for h in hits):
            continue
        bounds.append((start, m.start()))
        start = m.end()
    bounds.append((start, len(text)))
    return bounds


def _clause_of(pos: int, bounds: list[tuple[int, int]]) -> tuple[int, int]:
    for s, e in bounds:
        if s <= pos <= e:
            return s, e
    return bounds[-1]


def _is_birth_candidate(hit: _DateHit, clause: str, ref: date) -> bool:
    if BIRTH_KEYWORD.search(clause):
        return True
    return hit.year <= ref.year - 10


def _find_gender(*texts: str) -> str | None | bool:
    """성별을 찾는다. 같은 범위에서 남·여가 모두 나오면 False(모순)를 돌려준다."""
    for t in texts:
        found = {g for g, pat in GENDER_PATTERNS.items() if pat.search(t)}
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            return False
    return None


def _find_calendar(*texts: str) -> str | None | bool:
    for t in texts:
        solar, lunar = "양력" in t, "음력" in t
        if solar and lunar:
            return False
        if solar or lunar:
            return "solar" if solar else "lunar"
    return None


def _find_time(clause: str) -> tuple[int | None, int | None, tuple[int, int] | None, bool]:
    """(시, 분, 원문 구간, 모호 여부). 구간은 clause 기준 위치."""
    m = AMPM_TIME.search(clause)
    if m:
        h, ampm = int(m["h"]), m["ampm"]
        minute = int(m["min"]) if m["min"] else (30 if m["half"] else 0)
        if ampm in ("오후", "낮", "저녁", "밤") and h < 12:
            h += 12
        if ampm in ("오전", "새벽", "아침") and h == 12:
            h = 0
        if ampm == "밤" and h == 24:
            h = 0
        return h % 24, minute, m.span(), False
    m = COLON_TIME.search(clause)
    if m and int(m["h"]) <= 23 and int(m["min"]) <= 59:
        return int(m["h"]), int(m["min"]), m.span(), False
    m = NAMED_TIME.search(clause)
    if m:
        return (12 if m["name"] == "정오" else 0), 0, m.span(), False
    m = BARE_TIME.search(clause)
    if m:
        h = int(m["h"])
        minute = int(m["min"]) if m["min"] else (30 if m["half"] else 0)
        if 13 <= h <= 23 or h == 0:      # 24시간 표기라 오전/오후가 분명함
            return h, minute, m.span(), False
        return None, None, m.span(), True
    return None, None, None, False


def _valid(hit: _DateHit, calendar: str) -> bool:
    if not 1 <= hit.month <= 12:
        return False
    if calendar == "lunar":
        return 1 <= hit.day <= 30
    try:
        date(hit.year, hit.month, hit.day)
    except ValueError:
        return False
    return True


def _mask(text: str, spans: list[tuple[int, int, str]]) -> str:
    out, last = [], 0
    for s, e, rep in sorted(spans):
        if s < last:
            continue
        out.append(text[last:s])
        out.append(rep)
        last = e
    out.append(text[last:])
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def parse(text: str, reference_date: str | None = None) -> ParseResult:
    """질문 문장에서 출생 정보를 추출한다.

    Args:
        text: 사용자 질문 원문
        reference_date: 기준일 YYYY-MM-DD (미래 날짜 판정용, 기본은 config.REFERENCE_DATE)
    """
    ref = date.fromisoformat(reference_date or config.REFERENCE_DATE)
    hits = _find_dates(text)
    bounds = _clauses(text, hits)
    mask_spans: list[tuple[int, int, str]] = []

    # 2자리 연도는 생년월일로 보이면 되묻는다 (세기를 추측하지 않음)
    two_digit = [m for pat in TWO_DIGIT_PATTERNS for m in pat.finditer(text)
                 if not any(h.start <= m.start() < h.end for h in hits)]
    for m in two_digit:
        mask_spans.append((m.start(), m.end(), PLACEHOLDER))

    candidates = []
    for h in hits:
        cs, ce = _clause_of(h.start, bounds)
        if _is_birth_candidate(h, text[cs:ce], ref):
            candidates.append((h, cs, ce))
            mask_spans.append((h.start, h.end, PLACEHOLDER))

    if not candidates:
        if two_digit:
            return ParseResult(None, _mask(text, mask_spans), "two_digit_year")
        return ParseResult(None, text)

    distinct = {(h.year, h.month, h.day) for h, _, _ in candidates}
    if len(distinct) > 1:
        return ParseResult(None, _mask(text, mask_spans), "multiple_birth_dates")

    hit, cs, ce = candidates[0]
    clause = text[cs:ce]

    # 출생 시각: 생년월일이 있는 구절에서만 찾는다 (계획 날짜·시간과 섞이지 않게)
    hour, minute, tspan, ambiguous = _find_time(clause)
    if tspan:
        mask_spans.append((cs + tspan[0], cs + tspan[1], ""))
    masked = _mask(text, mask_spans)
    if ambiguous:
        return ParseResult(None, masked, "ambiguous_hour")

    calendar = "lunar" if hit.leap else _find_calendar(clause, text)
    if calendar is None or calendar is False:
        return ParseResult(None, masked, "calendar_unknown")
    leap = hit.leap or (calendar == "lunar" and "윤달" in clause)

    if not _valid(hit, calendar) or not 1900 <= hit.year:
        return ParseResult(None, masked, "invalid_date")
    if (hit.year, hit.month, hit.day) > (ref.year, ref.month, ref.day):
        return ParseResult(None, masked, "future_date")

    gender = _find_gender(clause, text)
    if not gender:
        return ParseResult(None, masked, "gender_missing")

    notes = []
    if hour is None:
        notes.append("hour_unknown" if UNKNOWN_TIME.search(text) else "hour_absent")
    birth = BirthInfo(hit.year, hit.month, hit.day, calendar, leap, gender, hour, minute)
    return ParseResult(birth, masked, None, notes)
