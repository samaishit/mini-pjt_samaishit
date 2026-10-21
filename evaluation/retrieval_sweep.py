"""하이브리드 검색 가중치 비교 (BM25:벡터 = 0.2/0.5/0.8, 참고로 단독 검색도).

규칙 문서 검색만 따로 잰다. 질문마다 BM25·벡터 상위 3건을 한 번씩 받아 두고
EnsembleRetriever 와 같은 가중 RRF(c=60)로 다시 합쳐 상위 3건을 비교한다.

질문 집합:
  - 평가셋에서 규칙 문서를 기대하는 행 (references.json 의 rule:<topic>, 접두어 일치)
  - 아래 PROBES: 규칙 문서 제목을 그대로 쓰지 않은 바꿔 말한 질문 (정답 doc_id 1개)

실행: python -m evaluation.retrieval_sweep
"""
import csv
import json

from src import config
from src.birth_parser import parse
from src.retriever import RRF_C, TOP_K, _bm25, _vectorstore, expand_query

EVAL_DIR = config.PROJECT_ROOT / "evaluation"
WEIGHTS = [(0.2, 0.8), (0.5, 0.5), (0.8, 0.2), (1.0, 0.0), (0.0, 1.0)]

PROBES = [
    ("나를 도와주는 기운이 뭔지 알고 싶어", "rule:yongsin:overview"),
    ("필요한 오행을 일상에서 어떻게 써먹어?", "rule:yongsin:usage"),
    ("사주가 강한 편인지 약한 편인지는 어떻게 봐?", "rule:yongsin:strength"),
    ("부족한 기운 하나로 인생이 다 결정되는 거야?", "rule:yongsin:limits"),
    ("한 달 단위 운의 흐름은 어떻게 계산돼?", "rule:wolun:wolun"),
    ("올해 전체의 운은 무엇으로 보나요", "rule:wolun:seyun"),
    ("사주에서 한 달은 1일부터 시작하는 게 아니야?", "rule:wolun:jeolgi"),
    ("양력 5월이면 사주로는 몇 월이야?", "rule:wolun:mapping"),
    ("어느 달이 좋고 어느 달을 조심해야 하는지 판단 기준", "rule:wolun:good-caution"),
    ("자오가 부딪히면 어떻게 돼?", "rule:chunghap:yukchung"),
    ("충이 든 달에는 어떤 마음가짐이 좋을까", "rule:chunghap:attitude"),
    ("서로 끌어당기는 지지 조합", "rule:chunghap:yukhap"),
    ("불의 기운은 어느 쪽 방향이야?", "rule:direction:oheng"),
    ("물 기운이 많은 환경은 어떤 곳이야", "rule:direction:environment"),
    ("북동쪽이나 남서쪽 같은 방향은 어떻게 봐", "rule:direction:intercardinal"),
    ("물과 나무의 관계, 서로 살려주는 순서", "rule:oheng:generating"),
    ("쇠가 나무를 이긴다는 게 무슨 뜻이야", "rule:oheng:controlling"),
    ("돈과 관련된 십성", "rule:sipsung:jaeseong"),
    ("직장이나 명예를 뜻하는 십성", "rule:sipsung:gwanseong"),
    ("이사 날짜는 어떻게 잡는 게 좋아?", "rule:residence:move-timing"),
    ("살 동네를 사주로 고르는 방법", "rule:residence:selection"),
    ("여행 가기 좋은 시기를 운에 맞춰 정하려면", "rule:travel:timing"),
    ("내가 세운 여행 계획이 좋은지 어떻게 판정해?", "rule:travel:plan-rating"),
    ("태어난 시간을 모르면 결과가 달라져?", "rule:guidance:unknown-time"),
    ("왜 오늘부터 12개월만 보여줘?", "rule:guidance:reference-date"),
]


def eval_questions() -> list[tuple[str, str]]:
    refs = json.loads((EVAL_DIR / "references.json").read_text(encoding="utf-8"))
    with open(EVAL_DIR / "test_queries.csv", encoding="utf-8") as f:
        questions = {int(r["id"]): r["input"] for r in csv.DictReader(f)}
    out = []
    for qid, ref in refs.items():
        if qid == "_meta":
            continue
        for target in ref["expected_doc_ids"]:
            if target.startswith("rule:"):
                out.append((parse(questions[int(qid)]).masked_text, target))
    return out


def fuse(bm25_ids: list[str], vec_ids: list[str], w: tuple[float, float]) -> list[str]:
    scores: dict[str, float] = {}
    for weight, ids in zip(w, (bm25_ids, vec_ids)):
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (rank + RRF_C)
    order = list(dict.fromkeys(bm25_ids + vec_ids))   # 동점은 먼저 나온 순서 유지
    return sorted(order, key=lambda d: -scores[d])[:TOP_K]


def main():
    bm25 = _bm25("rule", TOP_K)
    db = _vectorstore()
    rows = []
    for source, items in (("eval", eval_questions()), ("probe", PROBES)):
        for question, target in items:
            q = expand_query(question)
            b = [d.metadata["doc_id"] for d in bm25.invoke(q)]
            v = [d.metadata["doc_id"] for d in db.similarity_search(q, k=TOP_K, filter={"doc_type": "rule"})]
            rows.append({"source": source, "question": question, "target": target, "bm25": b, "vector": v})

    results = []
    for w in WEIGHTS:
        hits, rr = 0, 0.0
        for r in rows:
            ranked = fuse(r["bm25"], r["vector"], w)
            rank = next((i for i, d in enumerate(ranked, 1) if d.startswith(r["target"])), None)
            hits += rank is not None
            rr += 1 / rank if rank else 0
        results.append({"weights": w, "hit@3": round(hits / len(rows), 3), "mrr@3": round(rr / len(rows), 3)})

    out = {"n": len(rows), "k": TOP_K, "c": RRF_C, "results": results, "rows": rows}
    (EVAL_DIR / "runs" / "retrieval_sweep.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
    print(f"질문 {len(rows)}개 (평가셋 {sum(r['source'] == 'eval' for r in rows)} + 바꿔 말한 질문 {len(PROBES)})")
    print("| BM25:벡터 | hit@3 | MRR@3 |\n|---|---|---|")
    for r in results:
        print(f"| {r['weights'][0]}:{r['weights'][1]} | {r['hit@3']} | {r['mrr@3']} |")


if __name__ == "__main__":
    main()
