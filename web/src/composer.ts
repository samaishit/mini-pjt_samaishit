// composer.ts - 출생 정보 폼 값을 질문 문장으로 조립한다.
// 서버의 결정론 파서(src/birth_parser.py)가 읽는 형식을 그대로 쓴다:
//   "양력 1988년 11월 3일 오전 7시 출생 남성입니다. <질문>"
//   "음력 1993년 윤3월 10일 오후 2시 30분 출생 여성입니다. <질문>"
//   "음력 1990년 7월 15일 출생 여성입니다. 태어난 시간은 몰라요. <질문>"
// 이 형식과 파서 결과의 대응은 composer.cases.json 에 적고, vitest 와 Python 테스트가 함께 확인한다.

export type Calendar = "solar" | "lunar";
export type Gender = "남" | "여";

export interface BirthFields {
  year: number;
  month: number;
  day: number;
  calendar: Calendar;
  leap: boolean; // 음력 윤달 (양력이면 무시)
  timeKnown: boolean;
  hour: number; // 0~23 (timeKnown 일 때만 사용)
  minute: number; // 0~59
  gender: Gender;
}

export const MAX_QUESTION_CHARS = 1000; // 서버 config.MAX_QUESTION_CHARS 와 같다

export function timePhrase(hour: number, minute: number): string {
  const ampm = hour < 12 ? "오전" : "오후";
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${ampm} ${h12}시${minute ? ` ${minute}분` : ""}`;
}

export function birthSentence(b: BirthFields): string {
  const cal = b.calendar === "solar" ? "양력" : "음력";
  const leap = b.calendar === "lunar" && b.leap ? "윤" : "";
  const time = b.timeKnown ? ` ${timePhrase(b.hour, b.minute)}` : "";
  const gender = b.gender === "남" ? "남성" : "여성";
  const sentence = `${cal} ${b.year}년 ${leap}${b.month}월 ${b.day}일${time} 출생 ${gender}입니다.`;
  return b.timeKnown ? sentence : `${sentence} 태어난 시간은 몰라요.`;
}

export function composeQuestion(b: BirthFields, question: string): string {
  return `${birthSentence(b)} ${question.trim()}`;
}

/** 폼 값의 기본 검증. 실제 날짜·윤달 존재 여부는 서버 파서가 다시 확인하고 되묻는다. */
export function validateBirth(b: BirthFields, today: Date = new Date()): string | null {
  const thisYear = today.getFullYear();
  if (!Number.isInteger(b.year) || b.year < 1900 || b.year > thisYear) return `출생 연도는 1900~${thisYear} 사이로 입력해 주세요.`;
  if (!Number.isInteger(b.month) || b.month < 1 || b.month > 12) return "출생 월은 1~12 사이로 입력해 주세요.";
  const maxDay = b.calendar === "lunar" ? 30 : new Date(b.year, b.month, 0).getDate();
  if (!Number.isInteger(b.day) || b.day < 1 || b.day > maxDay) return `출생 일은 1~${maxDay} 사이로 입력해 주세요.`;
  if (b.timeKnown && (b.hour < 0 || b.hour > 23 || b.minute < 0 || b.minute > 59)) return "출생 시각을 확인해 주세요.";
  return null;
}
