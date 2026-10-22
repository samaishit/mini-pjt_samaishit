# glossary.py - 답변에 나온 명리 용어를 일상어로 풀어 주는 '용어 풀이' (LLM 없이, 결정론)
"""풀이 문장은 data/rules/*.md 의 해당 절 내용을 쉬운 말로 옮긴 것이고, 그 절의 doc_id 를 근거로 붙인다.
답변 문장(한자 원문)에서 용어가 실제로 나온 것만, 처음 나온 순서대로 고른다.
"""
import re

# (표시 이름, 찾는 패턴, 풀이, 근거 doc_id)
TERMS = [
    ("원국", r"원국",
     "태어난 해·달·날·시의 네 기둥(연주·월주·일주·시주), 여덟 글자로 이루어진 타고난 사주 바탕입니다.",
     "rule:oheng:overview"),
    ("일간", r"일간",
     "태어난 날의 천간으로, 사주에서 '나 자신'을 뜻합니다. 십성은 모두 일간을 기준으로 봅니다.",
     "rule:sipsung:overview"),
    ("신강·신약", r"신강|신약",
     "신강은 나를 돕는 기운이 많아 내가 강한 사주, 신약은 나를 누르거나 빼는 기운이 많아 내가 약한 사주입니다. "
     "좋고 나쁨이 아니라 균형을 맞출 방향을 정하는 기준입니다.",
     "rule:yongsin:strength"),
    ("용신", r"용신",
     "내 사주에서 지나치거나 부족한 기운을 조절해 균형을 잡아 주는, 말하자면 '나에게 필요한 기운'입니다. "
     "그 기운의 방위·계절·환경을 가까이하면 좋다고 봅니다.",
     "rule:yongsin:overview"),
    ("오행", r"오행",
     "목(木)·화(火)·토(土)·금(金)·수(水) 다섯 가지 기운입니다. 사주의 여덟 글자는 모두 이 가운데 하나에 속합니다.",
     "rule:oheng:overview"),
    ("세운", r"세운",
     "해마다 들어오는 한 해의 기운입니다. 명리에서 한 해는 1월 1일이 아니라 입춘(2월 4일 무렵)에 바뀝니다.",
     "rule:wolun:seyun"),
    ("월운", r"월운",
     "한 달 단위로 바뀌는 기운으로, 그해 흐름(세운) 안에서 움직이는 작은 흐름입니다.",
     "rule:wolun:wolun"),
    ("절기 월", r"절기 월|절입",
     "명리의 한 달은 양력 1일이 아니라 입춘·경칩 같은 절기가 시작되는 날(절입일)에 바뀝니다. "
     "그래서 답변의 달은 대개 그달 초순부터 다음 달 초순까지입니다.",
     "rule:wolun:jeolgi"),
    ("비견·겁재", r"비견|겁재|비겁",
     "나와 같은 기운으로 동료·경쟁·자립심을 뜻합니다. 사람들과 함께하거나 스스로 결정할 일이 늘어나는 흐름입니다.",
     "rule:sipsung:bigyeop"),
    ("식신·상관", r"식신|상관(?!없|이 없|관계)|식상",
     "내가 만들어 내는 기운으로 표현·재능·즐거움을 뜻합니다. 취미·여행·창작처럼 나를 드러내고 즐기는 활동에 어울립니다.",
     "rule:sipsung:siksang"),
    ("편재·정재", r"편재|정재|재성",
     "내가 다스리는 기운으로 활동 범위·실속·생활 기반을 뜻합니다. 바깥 활동과 이동이 늘어나는 흐름이며, 투자 판단과는 관계없습니다.",
     "rule:sipsung:jaeseong"),
    ("편관·정관", r"편관|정관|관성",
     "나를 다스리는 기운으로 책임·규칙·직장 일을 뜻합니다. 맡은 일이 늘어 일정과 컨디션 관리가 중요해집니다.",
     "rule:sipsung:gwanseong"),
    ("편인·정인", r"편인|정인|인성",
     "나를 도와주는 기운으로 배움·보호·휴식을 뜻합니다. 공부·정리·조용한 재충전에 어울립니다.",
     "rule:sipsung:inseong"),
    ("충", r"일지와 충|일지 충|충하는|충이 있|충\(",
     "서로 정반대 자리의 기운이 부딪치는 관계로, 이동·변동이 많아지는 때를 뜻합니다. 나쁜 일이 생긴다는 뜻이 아니라 "
     "일정에 여유를 두고 중요한 결정을 한 번 더 점검하라는 신호입니다.",
     "rule:chunghap:yukchung"),
]
COMPILED = [(name, re.compile(pat), plain, doc) for name, pat, plain, doc in TERMS]

# 격국 이름별 중심 기운 (rule:yongsin:geukguk)
GEUKGUK_CORE = [("종왕", "한 기운이 매우 강해 그 흐름을 따르는 구조"), ("비겁", "나와 같은 기운(비견·겁재)이 중심인 구조"),
                ("식상", "표현·재능의 기운(식신·상관)이 중심인 구조"), ("재", "활동·실속의 기운(재성)이 중심인 구조"),
                ("관", "책임·규칙의 기운(관성)이 중심인 구조"), ("인", "배움·보호의 기운(인성)이 중심인 구조")]
MAX_TERMS = 10


def find_terms(text: str, geukguk: str | None = None) -> list[tuple[str, str, str]]:
    """답변 문장에 나온 용어의 (표시 이름, 풀이, doc_id) 를 처음 나온 순서대로 돌려준다."""
    hits = []
    plain_text = text.replace(geukguk, " " * len(geukguk)) if geukguk else text   # 격국 이름 속 글자(예: 비겁격의 비겁)는 따로 풀이
    for name, rx, plain, doc in COMPILED:
        m = rx.search(plain_text)
        if m:
            hits.append((m.start(), name, plain, doc))
    if geukguk and geukguk in text:
        core = next((c for key, c in GEUKGUK_CORE if geukguk.startswith(key)), "사주의 전체 구조를 나눈 유형")
        hits.append((text.index(geukguk), geukguk,
                     f"격국은 사주의 전체 구조를 몇 가지 유형으로 나눈 것이고, {geukguk}은 {core}입니다.", "rule:yongsin:geukguk"))
    return [(name, plain, doc) for _, name, plain, doc in sorted(hits)[:MAX_TERMS]]
