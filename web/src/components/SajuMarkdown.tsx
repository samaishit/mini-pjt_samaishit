import { Children, isValidElement, cloneElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";

// 오행 한자 → 화면 색 이름 (styles.css 의 .el-wood 등)
const ELEMENT_CLASS: Record<string, string> = { 木: "wood", 火: "fire", 土: "earth", 金: "metal", 水: "water" };
// 천간·지지의 오행 (간지 표기에 그 오행 색을 입힌다)
const CHAR_ELEMENT: Record<string, string> = {
  甲: "木", 乙: "木", 丙: "火", 丁: "火", 戊: "土", 己: "土", 庚: "金", 辛: "金", 壬: "水", 癸: "水",
  寅: "木", 卯: "木", 巳: "火", 午: "火", 辰: "土", 戌: "土", 丑: "土", 未: "土", 申: "金", 酉: "金", 亥: "水", 子: "水",
};
// 답변 속 '토(土)', '병화(丙火)', '무술(戊戌)' 같은 한글(한자) 표기
const MARK_RE = /([가-힣]{1,2}\((?:[甲乙丙丁戊己庚辛壬癸][木火土金水]|[木火土金水]|[甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥]{1,2})\))/g;

const SECTION_CLASS: Record<string, string> = {
  "한눈에 보기": "sec-glance", "사주 요약": "sec-summary", "용어 풀이": "sec-glossary", "근거 자료": "sec-source",
};

/** 문자열 속 오행·간지 표기를 색 있는 span 으로 바꾼다. */
export function markElements(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(MARK_RE)) {
    const mark = m[0];
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const inner = mark.slice(mark.indexOf("(") + 1, -1);
    const element = [...inner].find((c) => c in ELEMENT_CLASS);
    if (element && (inner.length === 1 || inner.length === 2 && element === inner[1])) {
      // 오행(土) · 천간+오행(丙火)
      out.push(<span key={start} className={`el el-${ELEMENT_CLASS[element]}`}>{mark}</span>);
    } else {
      // 간지(戊戌): 천간의 오행 색 + 명조체
      const el = CHAR_ELEMENT[inner[0]];
      out.push(<span key={start} className={`gz el-${ELEMENT_CLASS[el] ?? "earth"}`} title={`${inner} · ${el ?? ""}`}>{mark}</span>);
    }
    last = start + mark.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function decorate(children: ReactNode): ReactNode {
  return Children.map(children, (child) => {
    if (typeof child === "string") return markElements(child);
    if (isValidElement<{ children?: ReactNode }>(child) && child.props.children) {
      return cloneElement(child, undefined, decorate(child.props.children));
    }
    return child;
  });
}

function textOf(children: ReactNode): string {
  return Children.toArray(children).map((c) => (typeof c === "string" ? c : "")).join("").trim();
}

/** 답변 마크다운: 섹션 제목에 섹션별 모양을, 본문 오행·간지 표기에 오방색을 입힌다. */
export function SajuMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      components={{
        h3: ({ children: c }) => <h3 className={SECTION_CLASS[textOf(c)] ?? undefined}>{c}</h3>,
        li: ({ children: c }) => <li>{decorate(c)}</li>,
        p: ({ children: c }) => <p>{decorate(c)}</p>,
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
