import type { ReactNode } from "react";

interface Props {
  steps: ReactNode[];
  note?: ReactNode;
  title?: string;
}

/** 결과 창이 비었을 때의 안내 (단일 질의·대화 탭 공통 양식: 제목 + 번호 단계 + 작은 설명) */
export function Placeholder({ steps, note, title = "이렇게 물어보세요" }: Props) {
  return (
    <div className="placeholder">
      <h3>{title}</h3>
      <ol>
        {steps.map((s, i) => <li key={i}>{s}</li>)}
      </ol>
      {note && <p className="muted small">{note}</p>}
    </div>
  );
}
