import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { SajuMarkdown } from "./SajuMarkdown";

describe("SajuMarkdown: 오행·간지 표기에 오방색, 섹션 제목에 섹션 모양", () => {
  afterEach(cleanup);

  it("오행·천간+오행은 오행 색, 간지는 천간의 오행 색 + 명조", () => {
    const { container } = render(
      <SajuMarkdown>{"### 한눈에 보기\n- 무술(戊戌)월에는 필요한 기운 토(土)·수(水)와 병화(丙火)가 들어옵니다."}</SajuMarkdown>,
    );
    const marks = [...container.querySelectorAll(".el, .gz")].map((e) => [e.textContent, e.className]);
    expect(marks).toEqual([
      ["무술(戊戌)", "gz el-earth"],
      ["토(土)", "el el-earth"],
      ["수(水)", "el el-water"],
      ["병화(丙火)", "el el-fire"],
    ]);
    expect(container.querySelector("h3")?.className).toBe("sec-glance");
  });

  it("오행 표기가 없는 문장은 그대로", () => {
    const { container } = render(<SajuMarkdown>{"- 그대로 둡니다 (괄호도)."}</SajuMarkdown>);
    expect(container.querySelector(".el, .gz")).toBeNull();
    expect(container.textContent).toContain("그대로 둡니다 (괄호도).");
  });
});
