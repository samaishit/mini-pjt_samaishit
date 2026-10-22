import { describe, expect, it } from "vitest";
import { foldedMarkdown, type ChatView } from "./api";

describe("foldedMarkdown: 대화 탭에서 접은 문장을 섹션별로", () => {
  it("펼치지 않은 문장만, 앞에서 본 내용은 표시와 함께", () => {
    const view: ChatView = {
      mode: "focus", focus: ["여행 추천"], hidden_repeated: 1, hidden_out_of_focus: 1,
      sections: [
        { name: "한눈에 보기", claims: [{ text: "결론", origin: "llm", repeated: false, in_focus: true, shown: true }] },
        { name: "사주 요약", claims: [{ text: "원국은 …", origin: "code", repeated: true, in_focus: true, shown: false }] },
        { name: "이사 시기", claims: [{ text: "이사는 …", origin: "code", repeated: false, in_focus: false, shown: false }] },
      ],
    };
    expect(foldedMarkdown(view)).toBe("#### 사주 요약\n- 원국은 … _(앞에서 본 내용)_\n\n#### 이사 시기\n- 이사는 …");
  });
});
