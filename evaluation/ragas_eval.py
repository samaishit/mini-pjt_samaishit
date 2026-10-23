# ragas_eval.py - RAGAS 4지표 계산 (평가 전용 venv: .venv-eval, Python 3.12)
"""실행: .venv-eval\\Scripts\\python evaluation/ragas_eval.py --round 1

- 입력: runs/roundN.json (run_eval.py 가 저장한 응답), references.json (정답 요지), test_queries.csv
- 대상: guardrail 을 뺀 행 중 실행이 성공하고 contexts 가 있는 행
  (되묻기 응답처럼 LLM·검색 없이 끝난 행은 contexts 가 없어 RAGAS 가 정의되지 않으므로 제외하고 건수를 남긴다)
- user_input 은 <BIRTH> 로 치환한 질문을 쓴다 (원본 생년월일을 평가 LLM 에 보내지 않음)
- 결과: runs/roundN_ragas.json 저장, roundN_report.md 의 RAGAS 절을 채운다
- 빈 값 보충: --metrics faithfulness --only-missing --timeout 1800 (긴 답변이 시간 초과된 행만 다시 계산)
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
from ragas import EvaluationDataset, RunConfig, SingleTurnSample, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness  # noqa: E402

MODEL = os.getenv("JUDGE_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")   # 심사 모델과 같은 평가 LLM
# Bedrock 스로틀링으로 작업이 시간 초과되지 않도록 동시 실행을 줄이고 제한 시간을 늘린다
RUN_CONFIG = RunConfig(timeout=600, max_workers=4, max_retries=10, max_wait=90)
METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
RAGAS_MAX_TOKENS = 16000
MIN_VALID_SHARE = 0.75   # 유효 행이 대상의 75% 미만이면 '측정 불가'로 보고한다 (평균·라운드 비교 없음)


def masked_question(res: dict, fallback: str) -> str:
    for t in res.get("trace", []):
        if t.get("step") == "parse:birth":
            return t.get("input") or fallback
    return fallback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--ids", help="쉼표로 구분한 id (점검용, 결과 파일·리포트는 쓰지 않음)")
    parser.add_argument("--metrics", help="일부 지표만 다시 계산해 기존 결과에 합친다 (예: faithfulness)")
    parser.add_argument("--only-missing", action="store_true",
                        help="--metrics 와 함께: 기존 결과에서 값이 빈 행만 다시 계산한다 (시간 초과·한도 초과 행 보충)")
    parser.add_argument("--timeout", type=int, default=600, help="RAGAS 작업 하나의 제한 시간(초). 긴 답변의 faithfulness 는 늘린다")
    parser.add_argument("--tag", help="결과를 runs/roundN_ragas_<tag>.json 에 따로 저장하고 리포트는 건드리지 않는다 "
                                      "(평가 LLM 을 바꾼 참고 측정용, 예: JUDGE_MODEL=Haiku + --tag haiku)")
    args = parser.parse_args()
    if args.only_missing and not args.metrics:
        parser.error("--only-missing 은 --metrics 와 함께 쓴다")
    wanted = set(args.ids.split(",")) if args.ids else None

    results = json.loads((EVAL_DIR / "runs" / f"round{args.round}.json").read_text(encoding="utf-8"))["results"]
    refs = json.loads((EVAL_DIR / "references.json").read_text(encoding="utf-8"))
    with open(EVAL_DIR / "test_queries.csv", encoding="utf-8") as f:
        cases = {r["id"]: r for r in csv.DictReader(f)}

    samples, ids, skipped = [], [], []
    for cid, case in cases.items():
        if case["category"] == "guardrail" or cid not in refs or (wanted and cid not in wanted):
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

    # faithfulness 는 답변 문장마다 근거 판정을 JSON 으로 쓰므로, 답변이 길면 출력이 잘려 파싱에 실패한다 → 상한을 넉넉히
    llm = LangchainLLMWrapper(ChatBedrockConverse(model=MODEL, region_name="us-east-1", temperature=0,
                                                  max_tokens=RAGAS_MAX_TOKENS))
    emb = LangchainEmbeddingsWrapper(BedrockEmbeddings(model_id="amazon.titan-embed-text-v2:0", region_name="us-east-1"))
    all_metrics = {"faithfulness": faithfulness, "answer_relevancy": answer_relevancy,
                   "context_precision": context_precision, "context_recall": context_recall}
    run_metrics = [m.strip() for m in args.metrics.split(",")] if args.metrics else METRICS
    out_path = EVAL_DIR / "runs" / f"round{args.round}_ragas{'_' + args.tag if args.tag else ''}.json"
    prev = json.loads(out_path.read_text(encoding="utf-8")) if args.metrics and out_path.exists() else {}
    prev_rows = prev.get("per_row") or {}
    # 다시 계산하지 않은 행·지표는 기존 값을 유지한다
    per_row = {cid: dict(prev_rows.get(cid, {m: None for m in METRICS})) for cid in ids}
    run_ids = ([cid for cid in ids if any(per_row[cid].get(m) is None for m in run_metrics)]
               if args.only_missing else ids)
    run_samples = [s for cid, s in zip(ids, samples) if cid in run_ids]
    print(f"계산 대상 {len(run_ids)}건: {', '.join(run_ids) or '없음'}", flush=True)
    if run_samples:
        run_config = RunConfig(timeout=args.timeout, max_workers=RUN_CONFIG.max_workers,
                               max_retries=RUN_CONFIG.max_retries, max_wait=RUN_CONFIG.max_wait)
        result = evaluate(EvaluationDataset(samples=run_samples),
                          metrics=[all_metrics[m] for m in run_metrics],
                          llm=llm, embeddings=emb, run_config=run_config, show_progress=False,
                          raise_exceptions=False)
        for cid, (_, row) in zip(run_ids, result.to_pandas().iterrows()):
            per_row[cid].update({m: (None if row[m] is None or (isinstance(row[m], float) and math.isnan(row[m]))
                                     else round(float(row[m]), 3)) for m in run_metrics})
    means, valid = {}, {}
    for m in METRICS:
        vals = [v[m] for v in per_row.values() if v[m] is not None]
        valid[m] = len(vals)   # 오류(스로틀링 등)로 값이 비면 평균에서 빠지므로 유효 건수를 함께 남긴다
        means[m] = round(sum(vals) / len(vals), 3) if vals and len(vals) >= MIN_VALID_SHARE * len(ids) else None
    if wanted:
        print(json.dumps({"n": len(ids), "means": means, "valid": valid, "per_row": per_row}, ensure_ascii=False))
        return
    out = {"model": MODEL, "n": len(ids), "skipped_no_context": skipped, "means": means, "valid": valid,
           "per_row": per_row}
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    report = EVAL_DIR / f"round{args.round}_report.md"
    if report.exists() and not args.tag:   # 다른 평가 LLM 으로 낸 참고 결과는 리포트 표를 덮지 않는다
        table = ["| 지표 | 평균 | 유효 행 |", "|---|---|---|"]
        table += [f"| {m} | {means[m] if means[m] is not None else '측정 불가'} | {valid[m]}/{len(ids)} |" for m in METRICS]
        table += ["", f"- 유효 행이 대상의 {MIN_VALID_SHARE:.0%} 미만인 지표는 '측정 불가'로 두고 라운드 비교에서 뺀다",
                  f"- 대상 {len(ids)}건 (guardrail 제외, contexts 없는 되묻기 응답 제외: {', '.join(skipped) or '없음'})",
                  f"- 평가 LLM `{MODEL}`, 임베딩 Titan v2", "",
                  "| id | " + " | ".join(METRICS) + " |", "|---" * (len(METRICS) + 1) + "|"]
        table += [f"| {cid} | " + " | ".join(str(v[m]) for m in METRICS) + " |" for cid, v in per_row.items()]
        text = report.read_text(encoding="utf-8")
        text = re.sub(r"## RAGAS \(guardrail 제외\)\n.*?(?=\n## )", "## RAGAS (guardrail 제외)\n\n" + "\n".join(table) + "\n",
                      text, flags=re.DOTALL)
        report.write_text(text, encoding="utf-8")
    print(json.dumps({"n": len(ids), "means": means, "valid": valid, "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
