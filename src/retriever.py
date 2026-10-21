# retriever.py - 하이브리드 RAG 파이프라인 (BM25 + 벡터, 결정론 쿼리 확장)
"""명리 규칙 문서(rule)와 장소 DB(place)를 한 인덱스에서 검색합니다.

- 키워드: kiwi 형태소 BM25 (메모리, doc_type별로 미리 걸러서 생성)
- 벡터: Chroma(영속) + Bedrock Titan Embed v2, doc_type 메타데이터 필터
- 결합: EnsembleRetriever 0.5/0.5, RRF c=60, doc_id 기준 중복 제거
- doc_id: rule:<주제>:<이름>, 장소 id(R-..., T-...)

이 모듈은 LLM 모듈을 import하지 않습니다.
인덱스 생성: python -m src.retriever --build
"""
import argparse
import json
import re
from functools import lru_cache

from kiwipiepy import Kiwi
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from src import config

TOP_K = 3
WEIGHTS = (0.5, 0.5)  # (BM25, 벡터) - 1·2차 평가에서 0.2/0.5/0.8을 비교합니다
RRF_C = 60
DOC_TYPES = ("rule", "place")

_kiwi = Kiwi()
# 명리 용어가 쪼개지지 않도록 사용자 사전에 등록 (예: 용신 → 용/신 방지)
for _word in ("용신", "일간", "일지", "월운", "세운", "대운", "십성", "오행", "격국", "신강", "신약",
              "육충", "육합", "절기", "절입", "비겁", "식상", "재성", "관성", "인성", "시주", "만세력"):
    _kiwi.add_user_word(_word, "NNP")


def korean_tokenizer(text: str) -> list[str]:
    """한국어 형태소 토크나이저 (day2 검증본). 명사, 어간, 숫자, 외래어, 한자만 남깁니다."""
    keep = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN", "SH"}
    return [t.form for t in _kiwi.tokenize(text) if t.tag in keep]


# ---------------------------------------------------------------------------
# 문서 적재
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r"^## (?P<title>.+?)\n<!-- doc_id: (?P<doc_id>[\w:.-]+) -->\n(?P<body>.*?)(?=^## |\Z)",
                         re.MULTILINE | re.DOTALL)


def load_rule_docs() -> list[Document]:
    """data/rules/*.md 를 `## 제목` + doc_id 주석 단위로 잘라 문서로 만든다."""
    docs = []
    for path in sorted(config.RULES_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for m in _SECTION_RE.finditer(text):
            title, doc_id, body = m["title"].strip(), m["doc_id"], m["body"].strip()
            docs.append(Document(
                page_content=f"{title}\n{body}",
                metadata={"doc_id": doc_id, "doc_type": "rule", "topic": doc_id.split(":")[1],
                          "source": f"data/rules/{path.name}"},
            ))
    return docs


def _place_text(p: dict, kind: str) -> str:
    where = p.get("sido") or ("국내" if p.get("scope") == "domestic" else "해외")
    lines = [
        f"{p['name']} ({where}, {'주거 지역' if kind == 'region' else '여행지'})",
        f"방위: {p['direction']} / 오행: 주 {p['elements'][0]}, 보조 {p['elements'][1]}",
        p["description"],
        "특징: " + ", ".join(p["env_tags"]),
    ]
    if kind == "travel":
        lines.append("추천 월: " + ", ".join(f"{m}월" for m in p["best_months"]))
    return "\n".join(lines)


def load_place_docs() -> list[Document]:
    """장소 DB(JSON) 70건을 문서로 만든다. doc_id 는 장소 id 그대로 쓴다."""
    docs = []
    for kind, file, key in (("region", "regions.json", "regions"), ("travel", "travel.json", "travel")):
        rows = json.loads((config.PLACES_DIR / file).read_text(encoding="utf-8"))[key]
        for p in rows:
            docs.append(Document(
                page_content=_place_text(p, kind),
                metadata={"doc_id": p["id"], "doc_type": "place", "place_kind": kind,
                          "source": f"data/places/{file}"},
            ))
    return docs


@lru_cache(maxsize=1)
def load_all_docs() -> tuple[Document, ...]:
    return tuple(load_rule_docs() + load_place_docs())


# ---------------------------------------------------------------------------
# 인덱스와 검색기
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _vectorstore() -> Chroma:
    embeddings = BedrockEmbeddings(model_id=config.EMBED_MODEL, region_name=config.REGION)
    return Chroma(collection_name=config.COLLECTION, embedding_function=embeddings,
                  persist_directory=str(config.INDEX_DIR))


def build_index() -> int:
    """Chroma 인덱스를 새로 만든다. doc_id 를 벡터 id 로 써서 다시 실행해도 중복이 생기지 않는다."""
    db = _vectorstore()
    existing = db.get()["ids"]
    if existing:
        db.delete(ids=existing)
    docs = list(load_all_docs())
    db.add_documents(docs, ids=[d.metadata["doc_id"] for d in docs])
    return len(docs)


@lru_cache(maxsize=None)
def _bm25(doc_type: str | None, k: int) -> BM25Retriever:
    docs = [d for d in load_all_docs() if doc_type is None or d.metadata["doc_type"] == doc_type]
    bm25 = BM25Retriever.from_documents(docs, preprocess_func=korean_tokenizer)
    bm25.k = k
    return bm25


def _hybrid(doc_type: str | None, k: int) -> EnsembleRetriever:
    search_kwargs = {"k": k}
    if doc_type:
        search_kwargs["filter"] = {"doc_type": doc_type}
    vector = _vectorstore().as_retriever(search_kwargs=search_kwargs)
    return EnsembleRetriever(retrievers=[_bm25(doc_type, k), vector], weights=list(WEIGHTS),
                             c=RRF_C, id_key="doc_id")


# ---------------------------------------------------------------------------
# 결정론 쿼리 확장
# ---------------------------------------------------------------------------
ELEMENT_KEYWORDS = {
    "木": ["목(木)", "동쪽", "봄", "숲", "녹지"],
    "火": ["화(火)", "남쪽", "여름", "활기", "햇볕"],
    "土": ["토(土)", "중앙", "환절기", "평지", "안정"],
    "金": ["금(金)", "서쪽", "가을", "바위", "산업"],
    "水": ["수(水)", "북쪽", "겨울", "바다", "강", "호수"],
}


def expand_query(query: str, elements: list[str] | None = None, extra: list[str] | None = None) -> str:
    """질문 뒤에 용신 오행 키워드와 추가 키워드(월지, 방위 등)를 붙인다. LLM 을 쓰지 않는다."""
    words = []
    for e in elements or []:
        words += ELEMENT_KEYWORDS.get(e, [])
    words += extra or []
    seen = set()
    words = [w for w in words if not (w in seen or seen.add(w))]
    return f"{query} {' '.join(words)}".strip()


def retrieve_docs(query: str, doc_type: str | None = None, k: int = TOP_K,
                  elements: list[str] | None = None, extra_keywords: list[str] | None = None) -> list[Document]:
    """하이브리드 검색으로 상위 k개 문서를 돌려준다.

    Args:
        query: 검색 질문 (<BIRTH> 치환된 문장)
        doc_type: "rule"(명리 규칙), "place"(장소 설명), None(전체)
        k: 돌려줄 문서 수 (기본 3)
        elements: 쿼리 확장에 쓸 용신 오행 (예: ["火", "水", "木"])
        extra_keywords: 쿼리 확장에 덧붙일 키워드 (예: 월운 간지, 방위)
    """
    if doc_type is not None and doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type 은 {DOC_TYPES} 또는 None 이어야 합니다: {doc_type}")
    expanded = expand_query(query, elements, extra_keywords)
    return _hybrid(doc_type, k).invoke(expanded)[:k]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="하이브리드 RAG 인덱스 생성/검색 확인")
    parser.add_argument("--build", action="store_true", help="Chroma 인덱스를 새로 만든다")
    parser.add_argument("--query", help="검색해 볼 질문")
    parser.add_argument("--doc-type", choices=DOC_TYPES)
    args = parser.parse_args()
    if args.build:
        print(f"인덱스 생성 완료: {build_index()}건 → {config.INDEX_DIR}")
    if args.query:
        for d in retrieve_docs(args.query, args.doc_type):
            print(f"[{d.metadata['doc_id']}] {d.page_content[:60]!r}")
