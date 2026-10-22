import { describe, expect, it } from "vitest";
import cases from "./composer.cases.json";
import { birthSentence, composeQuestion, timePhrase, validateBirth, type BirthFields } from "./composer";
import { classify, type QueryResponse } from "./api";

describe("birthSentence: 서버 파서가 읽는 형식 (composer.cases.json 공유)", () => {
  for (const c of cases.cases) {
    it(c.sentence, () => {
      expect(birthSentence(c.fields as BirthFields)).toBe(c.sentence);
    });
  }

  it("질문을 뒤에 붙인다", () => {
    const b = cases.cases[0].fields as BirthFields;
    expect(composeQuestion(b, "  내년 5월 여행?  ")).toBe(`${cases.cases[0].sentence} 내년 5월 여행?`);
  });

  it("자정·정오를 오전/오후 12시로 쓴다", () => {
    expect(timePhrase(0, 0)).toBe("오전 12시");
    expect(timePhrase(12, 0)).toBe("오후 12시");
    expect(timePhrase(23, 59)).toBe("오후 11시 59분");
  });
});

describe("validateBirth", () => {
  const base = cases.cases[0].fields as BirthFields;
  const today = new Date(2026, 9, 22);
  it("정상 값은 통과", () => expect(validateBirth(base, today)).toBeNull());
  it("양력 2월 30일은 거부", () => expect(validateBirth({ ...base, month: 2, day: 30 }, today)).not.toBeNull());
  it("음력은 30일까지 허용 (실제 존재 여부는 서버가 되묻는다)", () =>
    expect(validateBirth({ ...base, calendar: "lunar", month: 2, day: 30 }, today)).toBeNull());
  it("미래 연도는 거부", () => expect(validateBirth({ ...base, year: 2027 }, today)).not.toBeNull());
  it("빈 연도는 거부", () => expect(validateBirth({ ...base, year: NaN }, today)).not.toBeNull());
});

describe("classify: trace 로 응답 종류 판별", () => {
  const res = (trace: QueryResponse["trace"]): QueryResponse => ({ answer: "", contexts: [], trace });
  it("입력 가드 차단은 거절", () =>
    expect(classify(res([{ step: "guard:input", input: "", output: "block: third_party" }])))
      .toEqual({ kind: "refusal", category: "third_party" }));
  it("파서 되묻기", () =>
    expect(classify(res([{ step: "parse:birth", input: "", output: "birth=none, clarification=invalid_date" },
                         { step: "guard:input", input: "", output: "allow: []" }])))
      .toEqual({ kind: "clarification", reason: "invalid_date" }));
  it("정상 답변", () =>
    expect(classify(res([{ step: "parse:birth", input: "", output: "birth=ok, clarification=None" },
                         { step: "guard:input", input: "", output: "allow: []" }])))
      .toEqual({ kind: "answer" }));
  it("503 은 일시 오류", () => expect(classify(res([]), 503)).toEqual({ kind: "error" }));
});
