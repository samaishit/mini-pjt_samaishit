# hanja.py - 간지·오행 한자를 '한글(한자)' 표기로 바꾼다 (LLM 없이, 결정론)
"""예: 戊戌월 → 무술(戊戌)월, 용신 土·火 → 용신 토(土)·화(火), 丙(화·火) → 병화(丙火)

- 이미 '무술(戊戌)'처럼 괄호 안에 들어 있는 한자는 그대로 둔다.
- 답변 검증(근거 doc_id, 기간 교정)은 한자 원문으로 한 뒤, 사용자에게 보여 주기 직전에만 바꾼다.
"""
import re

STEM_KO = {"甲": "갑", "乙": "을", "丙": "병", "丁": "정", "戊": "무", "己": "기", "庚": "경", "辛": "신", "壬": "임", "癸": "계"}
BRANCH_KO = {"子": "자", "丑": "축", "寅": "인", "卯": "묘", "辰": "진", "巳": "사", "午": "오", "未": "미",
             "申": "신", "酉": "유", "戌": "술", "亥": "해"}
ELEMENT_KO = {"木": "목", "火": "화", "土": "토", "金": "금", "水": "수"}
READING = {**STEM_KO, **BRANCH_KO, **ELEMENT_KO}
HANJA = "".join(READING)

# LLM 이 자주 쓰는 '丙(화·火)' 형태 → '병화(丙火)'
STEM_WITH_ELEMENT_RE = re.compile(rf"([{''.join(STEM_KO)}])\(\s*[가-힣]\s*[·,/]\s*([{''.join(ELEMENT_KO)}])\s*\)")
# '(戊戌)'처럼 괄호 안에 한자만 있는 표기(이미 한글 읽기가 붙은 것)는 그대로 두고, 나머지 한자 묶음만 바꾼다
HANJA_RUN_RE = re.compile(rf"\([{HANJA}]+\)|[{HANJA}]+")


def reading(run: str) -> str:
    return "".join(READING[c] for c in run)


def annotate(text: str) -> str:
    """간지·오행 한자를 '한글(한자)'로 바꾼다. 이미 바뀐 표기는 건드리지 않는다."""
    text = STEM_WITH_ELEMENT_RE.sub(lambda m: f"{STEM_KO[m.group(1)]}{ELEMENT_KO[m.group(2)]}({m.group(1)}{m.group(2)})", text)
    return HANJA_RUN_RE.sub(lambda m: m.group(0) if m.group(0).startswith("(") else f"{reading(m.group(0))}({m.group(0)})",
                            text)
