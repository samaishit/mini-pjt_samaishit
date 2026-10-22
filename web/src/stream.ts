// stream.ts - 스트리밍 API(/query/stream, /chat/stream) 수신과 진행 상태 계산
// 서버는 Server-Sent Events 로 stage(단계 알림)·step(trace 한 줄)·result(최종 응답)·error 를 보낸다.
// POST 요청이라 EventSource 대신 fetch 의 스트림을 직접 읽어 SSE 를 해석한다.

import type { ChatResponse, ChatResult, QueryResponse, QueryResult, TraceStep } from "./api";

export interface StageEvent {
  stage: string;
  label: string;
  agents?: string[];
}

export type StreamEvent =
  | { kind: "stage"; data: StageEvent }
  | { kind: "step"; data: TraceStep };

/** SSE 텍스트 덩어리를 이벤트로 나눈다. 끝이 잘린 이벤트는 rest 로 돌려준다. */
export function parseSSE(buffer: string): { events: { event: string; data: string }[]; rest: string } {
  const events: { event: string; data: string }[] = [];
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // keepalive 주석
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data.push(line.slice(6));
    }
    if (data.length) events.push({ event, data: data.join("\n") });
  }
  return { events, rest };
}

async function streamPost<T>(url: string, body: unknown, onEvent: (e: StreamEvent) => void,
                             signal?: AbortSignal): Promise<{ ok: true; status: number; data: T } | { ok: false; status: number; message: string }> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, status: 0, message: "요청을 취소했습니다." };
    return { ok: false, status: 0, message: "서버에 연결하지 못했습니다. uvicorn 이 실행 중인지 확인해 주세요." };
  }
  if (res.status === 422) {
    const b = await res.json().catch(() => null);
    return { ok: false, status: 422, message: b?.detail ?? "질문 형식이 올바르지 않습니다." };
  }
  if (!res.ok || !res.body) return { ok: false, status: res.status, message: `서버 오류 (${res.status})` };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSSE(buffer);
      buffer = rest;
      for (const ev of events) {
        const data = JSON.parse(ev.data);
        if (ev.event === "result") return { ok: true, status: 200, data: data as T };
        if (ev.event === "error") return { ok: true, status: 503, data: { answer: data.message, contexts: [], trace: [], error_type: data.error_type } as T };
        if (ev.event === "stage" || ev.event === "step") onEvent({ kind: ev.event, data } as StreamEvent);
      }
    }
  } catch (e) {
    if ((e as Error).name === "AbortError") return { ok: false, status: 0, message: "요청을 취소했습니다." };
    return { ok: false, status: 0, message: "응답을 받는 중 연결이 끊겼습니다. 다시 시도해 주세요." };
  }
  return { ok: false, status: 0, message: "응답이 끝나기 전에 연결이 끊겼습니다." };
}

export function postQueryStream(question: string, onEvent: (e: StreamEvent) => void, signal?: AbortSignal): Promise<QueryResult> {
  return streamPost<QueryResponse>("/query/stream", { question }, onEvent, signal);
}

export function postChatStream(message: string, sessionId: string | null, onEvent: (e: StreamEvent) => void,
                               signal?: AbortSignal): Promise<ChatResult> {
  return streamPost<ChatResponse>("/chat/stream", { message, session_id: sessionId }, onEvent, signal);
}

// ---------------------------------------------------------------------------
// 진행 상태: 단계 체크리스트 · 진행률 · 최근 활동
// ---------------------------------------------------------------------------
export const STAGES = [
  { key: "input", label: "질문 확인" },
  { key: "saju", label: "사주 계산" },
  { key: "route", label: "전문가 선택" },
  { key: "agents", label: "전문가 분석" },
  { key: "compose", label: "답변 정리" },
  { key: "validate", label: "근거 검증" },
  { key: "finalize", label: "마무리" },
] as const;

export type StageKey = (typeof STAGES)[number]["key"];

/** 각 단계에 들어설 때의 진행률. 단계 안에서는 다음 단계 직전까지 천천히 차오른다. */
export const STAGE_PERCENT: Record<StageKey | "start" | "done", number> = {
  start: 3, input: 8, saju: 15, route: 32, agents: 36, compose: 72, validate: 88, finalize: 95, done: 100,
};

const AGENT_NAMES: Record<string, string> = {
  saju_agent: "사주 해석", travel_agent: "여행", residence_agent: "주거", plan_agent: "계획 평가",
};

export interface Progress {
  stage: StageKey | "start" | "done";
  percent: number; // 서버 알림 기준 진행률 (화면은 이 값을 향해 부드럽게 따라간다)
  label: string; // 지금 하는 일
  log: string[]; // 최근 활동 (사람이 읽는 문장)
  agents: string[]; // 이번 질문에 부른 전문가
  agentsStarted: number;
  steps: number;
}

export const INITIAL_PROGRESS: Progress = {
  stage: "start", percent: 0, label: "요청을 보내고 있어요", log: [], agents: [], agentsStarted: 0, steps: 0,
};

/** trace 한 줄을 사람이 읽는 활동 문장으로 (모르는 단계는 null) */
export function stepLabel(t: TraceStep): string | null {
  const name = t.name ?? "";
  if (t.step === "parse:birth") return "출생 정보를 읽고 생년월일을 가렸어요";
  if (t.step === "llm:input_guard") return "질문이 안전한지 확인했어요";
  if (t.step === "guard:input") return String(t.output).startsWith("block") ? "처리할 수 없는 요청이에요" : "질문 확인을 마쳤어요";
  if (t.step === "tool:calculate_saju") return "타고난 사주(원국)를 계산했어요";
  if (t.step === "tool:get_luck_calendar") return "앞으로 12개월의 운세 달력을 만들었어요";
  if (t.step === "memory:chart") return "대화에서 기억해 둔 사주를 썼어요";
  if (t.step === "retrieve") return "명리 해설 문서를 찾아 근거로 붙였어요";
  if (t.step.startsWith("agent:")) {
    const who = AGENT_NAMES[t.step.slice(6)];
    return who ? `${who} 전문가에게 맡겼어요` : null;
  }
  if (t.step === "agent_tool:recommend_travel" || t.step === "tool:recommend_travel") return "여행지 후보를 점수로 골랐어요";
  if (t.step === "agent_tool:recommend_residence" || t.step === "tool:recommend_residence") return "살기 좋은 지역 후보를 골랐어요";
  if (t.step === "agent_tool:evaluate_plan" || t.step === "tool:evaluate_plan") return "계획을 평가했어요";
  if (t.step === "llm:agent") return String(t.output).startsWith("tool_calls") ? null : "전문가가 보고를 마쳤어요";
  if (t.step === "llm:compose") return "답변 초안을 만들었어요";
  if (t.step === "llm:fallback") return "요청이 많아 다른 모델로 바꿔 이어서 하고 있어요";
  if (t.step === "guard:output") {
    if (name === "citation_repair") return "빠진 근거 표시를 채웠어요";
    if (name === "month_range_repair") return "절기 월 날짜를 계산값으로 맞췄어요";
    if (name === "output_guard") return String(t.output).endsWith("=0") ? "근거와 표현 검증을 통과했어요" : "검증에서 고칠 곳을 찾았어요";
    if (name === "code_claim_check") return "마지막 점검을 마쳤어요";
  }
  return null;
}

export function reduceProgress(p: Progress, e: StreamEvent): Progress {
  if (e.kind === "stage") {
    const { stage, label, agents } = e.data;
    const key = (stage in STAGE_PERCENT ? stage : p.stage) as Progress["stage"];
    return {
      ...p,
      stage: key,
      percent: Math.max(p.percent, STAGE_PERCENT[key] ?? p.percent),
      label,
      agents: agents ?? p.agents,
      log: [...p.log, label].slice(-6),
    };
  }
  const t = e.data;
  let { percent, stage, agentsStarted } = p;
  if (t.step === "tool:get_luck_calendar") percent = Math.max(percent, 28);
  if (t.step.startsWith("agent:")) {
    agentsStarted += 1;
    stage = "agents";
    const n = Math.max(p.agents.length, 1);
    percent = Math.max(percent, STAGE_PERCENT.agents + Math.round((34 * (agentsStarted - 0.5)) / n));
  }
  if (t.step === "llm:compose") percent = Math.max(percent, 85);
  const line = stepLabel(t);
  return {
    ...p,
    stage,
    percent: Math.min(percent, 99),
    agentsStarted,
    steps: p.steps + 1,
    label: line ?? p.label,
    log: line ? [...p.log, line].slice(-6) : p.log,
  };
}

/** 체크리스트의 각 단계 상태 */
export function stageStatus(p: Progress, key: StageKey): "done" | "active" | "pending" {
  if (p.stage === "done") return "done";
  const order = STAGES.map((s) => s.key) as string[];
  const cur = order.indexOf(p.stage === "start" ? "input" : p.stage);
  const idx = order.indexOf(key);
  return idx < cur ? "done" : idx === cur ? "active" : "pending";
}

/** 기다리는 동안 보여 줄 짧은 명리 상식 (규칙 문서 내용) */
export const TIPS = [
  "명리의 한 달은 양력 1일이 아니라 입춘·경칩 같은 절기가 시작되는 날에 바뀌어요.",
  "용신은 내 사주에서 넘치거나 부족한 기운을 맞춰 주는 '나에게 필요한 기운'이에요.",
  "한 해의 기운(세운)은 1월 1일이 아니라 입춘(2월 4일 무렵)에 바뀌어요.",
  "충(沖)이 있는 달은 나쁜 달이 아니라, 일정에 여유를 두고 한 번 더 점검하라는 신호예요.",
  "여행지는 필요한 기운의 오행과 방향, 그달이 제철인지를 점수로 따져 골라요.",
  "생년월일 원문은 AI에게 보내지 않아요. 계산된 간지만 근거로 써요.",
  "간지와 용신은 AI가 추측하지 않고 만세력 엔진이 계산한 값만 써요.",
];
