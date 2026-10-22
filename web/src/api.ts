// api.ts - POST /query 와 GET /examples 클라이언트, 응답 종류 판별

export interface Context {
  doc_id: string;
  text: string;
  source?: string;
  doc_type?: string;
}

export interface TraceStep {
  step: string;
  input: string;
  output: string;
  type?: string;
  name?: string;
  latency_ms?: number;
}

export interface QueryResponse {
  answer: string;
  contexts: Context[];
  trace: TraceStep[];
  total_latency_ms?: number;
  reference_date?: string;
  error_type?: string; // 503 일 때 오류 종류 (예: ThrottlingException)
}

export interface Example {
  id: number;
  category: "positive" | "negative" | "edge" | "guardrail";
  input: string;
}

export type QueryResult =
  | { ok: true; status: number; data: QueryResponse }
  | { ok: false; status: number; message: string };

export const CLIENT_TIMEOUT_MS = 200_000; // 서버 평가 기준 180초 + 여유

export async function postQuery(question: string, signal?: AbortSignal): Promise<QueryResult> {
  let res: Response;
  try {
    res = await fetch("/query", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ question }),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, status: 0, message: "요청을 취소했습니다." };
    return { ok: false, status: 0, message: "서버에 연결하지 못했습니다. uvicorn 이 실행 중인지 확인해 주세요." };
  }
  const body = await res.json().catch(() => null);
  if (res.status === 422) return { ok: false, status: 422, message: body?.detail ?? "질문 형식이 올바르지 않습니다." };
  if (!res.ok && !body?.answer) return { ok: false, status: res.status, message: `서버 오류 (${res.status})` };
  // 503 도 answer(고정 안내 문구)를 담아 오므로 그대로 보여 준다
  return { ok: true, status: res.status, data: body as QueryResponse };
}

export interface ChatSession {
  session_id: string;
  turns?: number;
  has_chart?: boolean;
  chart_brief?: string | null;
  last_question?: string | null;
  shown_places?: number;
  followup?: boolean;
  kind?: "answer" | "refusal" | "clarification";
}

export interface ViewClaim {
  text: string;
  origin: "llm" | "code";
  repeated: boolean; // 이 대화에서 이미 보여 준 문장
  in_focus: boolean; // 이번 질문의 범위 안
  shown: boolean; // 답변에 펼쳐 보여 줬는지
}

export interface ChatView {
  mode: "full" | "focus";
  focus: string[] | null;
  hidden_repeated: number;
  hidden_out_of_focus: number;
  sections: { name: string; claims: ViewClaim[] }[];
}

export interface ChatResponse extends QueryResponse {
  session: ChatSession;
  answer_full?: string; // 필터 전 전체 답변
  view?: ChatView;
}

/** 접어 둔 문장을 섹션별 마크다운으로 (화면의 '접은 내용 펼치기') */
export function foldedMarkdown(view: ChatView): string {
  const lines: string[] = [];
  for (const sec of view.sections) {
    const hidden = sec.claims.filter((c) => !c.shown);
    if (!hidden.length) continue;
    lines.push(`#### ${sec.name}`);
    for (const c of hidden) lines.push(`- ${c.text}${c.repeated ? " _(앞에서 본 내용)_" : ""}`);
    lines.push("");
  }
  return lines.join("\n").trim();
}

export type ChatResult =
  | { ok: true; status: number; data: ChatResponse }
  | { ok: false; status: number; message: string };

/** POST /chat: 대화 탭의 한 턴. session_id 가 없으면 서버가 새 대화를 연다. */
export async function postChat(message: string, sessionId: string | null, signal?: AbortSignal): Promise<ChatResult> {
  let res: Response;
  try {
    res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ message, session_id: sessionId }),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, status: 0, message: "요청을 취소했습니다." };
    return { ok: false, status: 0, message: "서버에 연결하지 못했습니다. uvicorn 이 실행 중인지 확인해 주세요." };
  }
  const body = await res.json().catch(() => null);
  if (res.status === 422) return { ok: false, status: 422, message: body?.detail ?? "메시지 형식이 올바르지 않습니다." };
  if (!res.ok && !body?.answer) return { ok: false, status: res.status, message: `서버 오류 (${res.status})` };
  return { ok: true, status: res.status, data: body as ChatResponse };
}

/** DELETE /chat/{id}: 대화를 끝내고 서버의 기억을 지운다. */
export async function endChat(sessionId: string): Promise<void> {
  try {
    await fetch(`/chat/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  } catch {
    // 서버가 꺼져 있어도 화면의 대화는 비운다
  }
}

export async function getExamples(): Promise<{ reference_date: string; examples: Example[] } | null> {
  try {
    const res = await fetch("/examples");
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export type AnswerKind =
  | { kind: "answer" }
  | { kind: "refusal"; category: string }
  | { kind: "clarification"; reason: string }
  | { kind: "error" };

/** trace 로 응답 종류를 판별한다: 입력 가드 차단 → 거절, 파서 되묻기 → 되묻기, 그 밖 → 답변 */
export function classify(res: QueryResponse, status = 200): AnswerKind {
  if (status >= 500) return { kind: "error" };
  for (const t of res.trace) {
    if (t.step === "guard:input" && t.output.startsWith("block:")) {
      return { kind: "refusal", category: t.output.slice("block:".length).trim() };
    }
  }
  const parse = res.trace.find((t) => t.step === "parse:birth");
  const m = parse?.output.match(/clarification=(\w+)/);
  if (m && m[1] !== "None") return { kind: "clarification", reason: m[1] };
  return { kind: "answer" };
}

export const REFUSAL_LABELS: Record<string, string> = {
  injection: "프롬프트 공격 차단",
  off_topic: "도메인 밖 요청",
  third_party: "제3자 개인정보",
  real_estate: "부동산 투자 조언",
  explicit: "선정적 요청",
};

export const DOC_TYPE_LABELS: Record<string, string> = {
  rule: "명리 규칙",
  place: "장소",
  saju: "사주 계산",
  plan: "계획 평가",
};
