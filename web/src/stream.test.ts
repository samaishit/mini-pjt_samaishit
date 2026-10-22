import { describe, expect, it } from "vitest";
import { INITIAL_PROGRESS, parseSSE, reduceProgress, stageStatus, stepLabel, type StreamEvent } from "./stream";

describe("parseSSE", () => {
  it("완성된 이벤트만 꺼내고 잘린 뒷부분은 남긴다 (keepalive 주석 무시)", () => {
    const buf = 'event: stage\ndata: {"stage":"input","label":"확인"}\n\n: keepalive\n\nevent: step\ndata: {"step":"gu';
    const { events, rest } = parseSSE(buf);
    expect(events).toEqual([{ event: "stage", data: '{"stage":"input","label":"확인"}' }]);
    expect(rest).toBe('event: step\ndata: {"step":"gu');
  });
});

describe("reduceProgress", () => {
  const run = (events: StreamEvent[]) => events.reduce(reduceProgress, INITIAL_PROGRESS);

  it("단계 알림과 trace 로 진행률·현재 할 일·최근 활동을 갱신한다", () => {
    const p = run([
      { kind: "stage", data: { stage: "input", label: "질문과 출생 정보를 확인하고 있어요" } },
      { kind: "step", data: { step: "parse:birth", input: "", output: "" } },
      { kind: "stage", data: { stage: "saju", label: "사주를 계산하고 있어요" } },
      { kind: "step", data: { step: "tool:get_luck_calendar", input: "", output: "" } },
      { kind: "stage", data: { stage: "route", label: "전문가를 골랐어요: 여행, 주거", agents: ["travel_agent", "residence_agent"] } },
      { kind: "step", data: { step: "agent:travel_agent", input: "", output: "handoff" } },
    ]);
    expect(p.stage).toBe("agents");
    expect(p.agents).toEqual(["travel_agent", "residence_agent"]);
    expect(p.agentsStarted).toBe(1);
    expect(p.percent).toBeGreaterThan(36);
    expect(p.percent).toBeLessThan(72);
    expect(p.label).toBe("여행 전문가에게 맡겼어요");
    expect(stageStatus(p, "saju")).toBe("done");
    expect(stageStatus(p, "agents")).toBe("active");
    expect(stageStatus(p, "compose")).toBe("pending");
  });

  it("진행률은 줄지 않고 완료 전에는 100% 가 되지 않는다", () => {
    const p = run([
      { kind: "stage", data: { stage: "compose", label: "정리 중" } },
      { kind: "step", data: { step: "llm:compose", input: "", output: "" } },
      { kind: "stage", data: { stage: "saju", label: "늦게 온 알림" } },
    ]);
    expect(p.percent).toBe(85);
    expect(run([{ kind: "stage", data: { stage: "done", label: "완료" } }]).percent).toBe(100);
  });
});

describe("stepLabel", () => {
  it("trace 단계를 사람이 읽는 문장으로, 모르는 단계는 null", () => {
    expect(stepLabel({ step: "llm:fallback", input: "", output: "" })).toContain("다른 모델");
    expect(stepLabel({ step: "guard:output", input: "", output: "violations=0", name: "output_guard" })).toBe("근거와 표현 검증을 통과했어요");
    expect(stepLabel({ step: "llm:agent", input: "", output: "tool_calls=['x']" })).toBeNull();
    expect(stepLabel({ step: "unknown", input: "", output: "" })).toBeNull();
  });
});
