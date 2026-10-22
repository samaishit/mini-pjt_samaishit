import { useState } from "react";
import { SajuMarkdown } from "./SajuMarkdown";
import { classify, DOC_TYPE_LABELS, REFUSAL_LABELS, type QueryResponse } from "../api";

interface Props {
  data: QueryResponse;
  status: number;
  elapsedMs: number;
}

const KIND_LABEL = {
  answer: "답변",
  refusal: "거절",
  clarification: "되묻기",
  error: "일시 오류",
} as const;

/** 답변(마크다운) + 근거 탭 + 처리 과정 탭 */
export function ResultView({ data, status, elapsedMs }: Props) {
  const [tab, setTab] = useState<"contexts" | "trace">("trace");
  const kind = classify(data, status);
  const detail =
    kind.kind === "refusal" ? REFUSAL_LABELS[kind.category] ?? kind.category :
    kind.kind === "clarification" ? `출생 정보 확인 필요 (${kind.reason})` :
    kind.kind === "error" && data.error_type ?
      `원인: ${data.error_type}${data.error_type === "ThrottlingException" ? " (Bedrock 호출 한도 초과, 잠시 후 다시 시도)" : ""}` : null;

  return (
    <section className="result" aria-live="polite">
      <header className="result-head">
        <span className={`badge kind-${kind.kind}`}>{KIND_LABEL[kind.kind]}</span>
        {detail && <span className="muted">{detail}</span>}
        <span className="spacer" />
        <span className="muted">{(elapsedMs / 1000).toFixed(1)}초</span>
      </header>
      <div className={`answer kind-${kind.kind}`}>
        <SajuMarkdown>{data.answer}</SajuMarkdown>
      </div>

      <nav className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "trace"} className={tab === "trace" ? "on" : ""}
                onClick={() => setTab("trace")}>
          처리 과정 <span className="count">{data.trace.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "contexts"} className={tab === "contexts" ? "on" : ""}
                onClick={() => setTab("contexts")}>
          근거 <span className="count">{data.contexts.length}</span>
        </button>
      </nav>

      {tab === "contexts" ? <ContextList data={data} /> : <TraceTimeline data={data} />}
    </section>
  );
}

function ContextList({ data }: { data: QueryResponse }) {
  if (!data.contexts.length) return <p className="muted empty">근거 문서가 없습니다 (거절·되묻기 응답은 contexts 가 비어 있습니다).</p>;
  return (
    <ul className="contexts">
      {data.contexts.map((c) => (
        <li key={c.doc_id}>
          <details>
            <summary>
              <code>{c.doc_id}</code>
              {c.doc_type && <span className={`badge type-${c.doc_type}`}>{DOC_TYPE_LABELS[c.doc_type] ?? c.doc_type}</span>}
              {c.source && <span className="muted small">{c.source === "rag" ? "검색" : "도구"}</span>}
              <span className="preview">{c.text.split("\n")[0]}</span>
            </summary>
            <pre>{c.text}</pre>
          </details>
        </li>
      ))}
    </ul>
  );
}

function TraceTimeline({ data }: { data: QueryResponse }) {
  return (
    <ol className="trace">
      {data.trace.map((t, i) => (
        <li key={i} className={`step type-${t.type ?? "other"}`}>
          <details>
            <summary>
              <span className="idx">{i + 1}</span>
              <code className="step-name">{t.step}</code>
              {t.name && t.name !== t.step.split(":")[1] && <span className="muted small">{t.name}</span>}
              <span className="spacer" />
              {t.latency_ms != null && <span className="muted small">{t.latency_ms.toLocaleString()} ms</span>}
            </summary>
            <dl>
              <dt>input</dt>
              <dd><pre>{t.input || "—"}</pre></dd>
              <dt>output</dt>
              <dd><pre>{t.output || "—"}</pre></dd>
            </dl>
          </details>
        </li>
      ))}
    </ol>
  );
}
