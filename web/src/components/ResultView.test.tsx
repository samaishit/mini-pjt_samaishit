import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ResultView } from "./ResultView";
import type { QueryResponse } from "../api";

const answer: QueryResponse = {
  answer: "### 여행 추천\n- 乙巳월(2027-05-06 ~ 2027-06-05)에는 해외 일본 교토를 추천합니다.\n\n※ 사주 해석은 참고용입니다.",
  contexts: [{ doc_id: "T-OS-KYOTO", text: "일본 교토 | 방위 동", source: "tool", doc_type: "place" }],
  trace: [
    { step: "parse:birth", input: "<BIRTH> 남성", output: "birth=ok, clarification=None", type: "parse", name: "birth_parser" },
    { step: "guard:input", input: "<BIRTH> 남성", output: "allow: []", type: "guard", name: "input_guard" },
    { step: "llm:compose", input: "", output: "tool_calls=['StructuredAnswer']", type: "llm", name: "compose", latency_ms: 7412 },
  ],
};

describe("ResultView", () => {
  afterEach(cleanup);

  it("답변을 마크다운으로, 처리 과정을 기본 탭으로 보여 준다", () => {
    render(<ResultView data={answer} status={200} elapsedMs={23400} />);
    expect(screen.getByRole("heading", { name: "여행 추천" })).toBeTruthy();
    expect(screen.getByText("답변")).toBeTruthy();
    expect(screen.getByText("23.4초")).toBeTruthy();
    expect(screen.getByText("llm:compose")).toBeTruthy();
    expect(screen.getByText("7,412 ms")).toBeTruthy();
  });

  it("근거 탭으로 바꾸면 doc_id 와 종류가 보인다", () => {
    render(<ResultView data={answer} status={200} elapsedMs={1000} />);
    fireEvent.click(screen.getByRole("tab", { name: /근거/ }));
    expect(screen.getByText("T-OS-KYOTO")).toBeTruthy();
    expect(screen.getByText("장소")).toBeTruthy();
  });

  it("거절 응답은 거절 배지와 분류를 표시한다", () => {
    const refusal: QueryResponse = {
      answer: "죄송합니다. 본인 외 다른 분의 사주나 개인정보는 처리할 수 없습니다.",
      contexts: [],
      trace: [{ step: "guard:input", input: "(원문 생략)", output: "block: third_party", type: "guard", name: "third_party" }],
    };
    render(<ResultView data={refusal} status={200} elapsedMs={50} />);
    expect(screen.getByText("거절")).toBeTruthy();
    expect(screen.getByText("제3자 개인정보")).toBeTruthy();
  });
});
