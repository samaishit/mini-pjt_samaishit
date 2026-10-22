import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatView } from "./ChatView";

function reply(followup: boolean, turns: number) {
  return {
    answer: "### 한눈에 보기\n- 내년 5월은 바깥 활동에 힘이 실리는 달입니다.",
    contexts: [],
    trace: [{ step: "guard:input", input: "", output: "allow: []" }],
    session: { session_id: "s1", turns, has_chart: true, chart_brief: "일간 壬, 용신 戊·丙", followup, kind: "answer" },
  };
}

describe("ChatView", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("첫 메시지에 출생 정보를 붙이고, 다음부터는 세션으로 이어 묻는다", async () => {
    const calls: { url: string; body?: { message: string; session_id: string | null } }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (init?.method === "DELETE") return new Response(JSON.stringify({ deleted: true }));
      // 스트리밍 API: 단계 알림 → 결과 (SSE)
      const sse = `event: stage\ndata: ${JSON.stringify({ stage: "input", label: "확인 중" })}\n\n` +
        `event: result\ndata: ${JSON.stringify(reply(calls.length > 1, calls.length))}\n\n`;
      return new Response(sse, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }));
    render(<ChatView />);

    fireEvent.change(screen.getByPlaceholderText("질문을 입력하세요"), { target: { value: "내년 5월 여행지?" } });
    fireEvent.click(screen.getByRole("button", { name: "보내기" }));
    await waitFor(() => expect(screen.getByText("일간 壬, 용신 戊·丙")).toBeTruthy());
    expect(calls[0].url).toBe("/chat/stream");
    expect(calls[0].body).toEqual({ message: "양력 1988년 11월 3일 오전 7시 출생 남성입니다. 내년 5월 여행지?", session_id: null });

    fireEvent.click(screen.getByRole("button", { name: "그럼 해외는?" }));   // 이어 묻기 칩
    await waitFor(() => expect(screen.getByText("이어 묻기")).toBeTruthy());
    expect(calls[1].body).toEqual({ message: "그럼 해외는?", session_id: "s1" });   // 출생 정보 없이 세션으로

    expect(screen.queryByText(/접은 내용 펼치기/)).toBeNull();   // view 가 없으면 접은 내용 영역도 없다

    fireEvent.click(screen.getByRole("button", { name: "새 대화" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/chat/s1")).toBe(true));
    expect(screen.queryByText("이어 묻기")).toBeNull();
  });
});
