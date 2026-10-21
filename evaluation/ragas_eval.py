# ragas_eval.py - RAGAS 4지표 계산 (평가 전용 venv: .venv-eval, Python 3.12)
"""실행: .venv-eval\\Scripts\\python evaluation/ragas_eval.py --round 1

- 입력: runs/roundN.json (run_eval.py 가 저장한 응답), references.json (정답 요지), test_queries.csv
- 대상: guardrail 을 뺀 행 중 실행이 성공하고 contexts 가 있는 행
  (되묻기 응답처럼 LLM·검색 없이 끝난 행은 contexts 가 없어 RAGAS 가 정의되지 않으므로 제외하고 건수를 남긴다)
- user_input 은 <BIRTH> 로 치환한 질문을 쓴다 (원본 생년월일을 평가 LLM 에 보내지 않음)
- 결과: runs/roundN_ragas.json 저장, roundN_report.md 의 RAGAS 절을 채운다
"""
import argparse
import csv
import json
import math
import os
import re
import warnings
from pathlib import Path

from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=DeprecationWarning)
EVAL_DIR = Path(__file__).resolve().parent
load_dotenv(EVAL_DIR.parent / ".env")

from langchain_aws import BedrockEmbeddings, ChatBedrockConverse  # noqa: E402
from ragas import EvaluationDataset, SingleTurnSample, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness  # noqa: E402

MODEL = os.getenv("SAJU_LLM_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


def masked_question(res: dict, fallback: str) -> str:
    for t in res.get("trace", []):
        if t.get("step") == "parse:birth":
            return t.get("input") or fallback
    return fallback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, required=True)
    args = parser.parse_args()

    results = json.loads((EVAL_DIR / "runs" / f"round{args.round}.json").read_text(encoding="utf-8"))["results"]
    refs = json.loads((EVAL_DIR / "references.json").read_text(encoding="utf-8"))
    with open(EVAL_DIR / "test_queries.csv", encoding="utf-8") as f:
        cases = {r["id"]: r for r in csv.DictReader(f)}

    samples, ids, skipped = [], [], []
    for cid, case in cases.items():
        if case["category"] == "guardrail" or cid not in refs:
            continue
        res = results.get(cid)
        if not res or res.get("status") != "ok" or not res.get("contexts"):
            skipped.append(cid)
            continue
        samples.append(SingleTurnSample(
            user_input=masked_question(res, case["input"]),
            response=res["answer"],
            retrieved_contexts=[f"[{c['doc_id']}] {c['text']}" for c in res["contexts"]],
            reference=refs[cid]["reference"],
        ))
        ids.append(cid)

    llm = LangchainLLMWrapper(ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0, max_tokens=4096))
    emb = LangchainEmbeddingsWrapper(BedrockEmbeddings(model_id="amazon.titan-embed-text-v2:0", region_name="us-east-1"))
    result = evaluate(EvaluationDataset(samples=samples),
                      metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
                      llm=llm, embeddings=emb, show_progress=False, raise_exceptions=False)
    df = result.to_pandas()
    per_row = {}
    for cid, (_, row) in zip(ids, df.iterrows()):
        per_row[cid] = {m: (None if row[m] is None or (isinstance(row[m], float) and math.isnan(row[m]))
                            else round(float(row[m]), 3)) for m in METRICS}
    means = {}
    for m in METRICS:
        vals = [v[m] for v in per_row.values() if v[m] is not None]
        means[m] = round(sum(vals) / len(vals), 3) if vals else None
    out = {"model": MODEL, "n": len(ids), "skipped_no_context": skipped, "means": means, "per_row": per_row}
    (EVAL_DIR / "runs" / f"round{args.round}_ragas.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                    encoding="utf-8")

    report = EVAL_DIR / f"round{args.round}_report.md"
    if report.exists():
        table = ["| 지표 | 평균 |", "|---|---|"] + [f"| {m} | {means[m]} |" for m in METRICS]
        table += ["", f"- 대상 {len(ids)}건 (guardrail 제외, contexts 없는 되묻기 응답 제외: {', '.join(skipped) or '없음'})",
                  f"- 평가 LLM `{MODEL}`, 임베딩 Titan v2", "",
                  "| id | " + " | ".join(METRICS) + " |", "|---" * (len(METRICS) + 1) + "|"]
        table += [f"| {cid} | " + " | ".join(str(v[m]) for m in METRICS) + " |" for cid, v in per_row.items()]
        text = report.read_text(encoding="utf-8")
        text = re.sub(r"## RAGAS \(guardrail 제외\)\n.*?(?=\n## )", "## RAGAS (guardrail 제외)\n\n" + "\n".join(table) + "\n",
                      text, flags=re.DOTALL)
        report.write_text(text, encoding="utf-8")
    print(json.dumps({"n": len(ids), "means": means, "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
