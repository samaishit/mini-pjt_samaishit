import { useEffect, useState } from "react";
import { STAGES, TIPS, stageStatus, type Progress } from "../stream";

interface Props {
  progress: Progress;
  elapsedMs: number;
  onCancel?: () => void;
  compact?: boolean; // 대화 탭 말풍선 안에서는 작게
}

/**
 * 답변을 기다리는 동안의 로딩 화면.
 * 서버 단계 알림으로 진행률·체크리스트·최근 활동을 갱신하고, 알림이 없는 동안에도
 * 진행률이 다음 단계 직전까지 천천히 차올라 멈춰 보이지 않게 한다.
 */
export function StreamProgress({ progress, elapsedMs, onCancel, compact }: Props) {
  const [shown, setShown] = useState(progress.percent);
  const [tip, setTip] = useState(() => Math.floor(Math.random() * TIPS.length));

  // 서버 진행률을 향해 부드럽게 따라가고, 알림이 없을 때도 다음 단계 직전(+9)까지 아주 천천히 오른다
  useEffect(() => {
    const id = window.setInterval(() => {
      setShown((v) => {
        const target = progress.percent;
        if (v < target) return Math.min(target, v + Math.max(1, (target - v) * 0.25));
        const ceiling = Math.min(target + 9, 98);
        return v < ceiling ? v + 0.15 : v;
      });
    }, 150);
    return () => window.clearInterval(id);
  }, [progress.percent]);

  useEffect(() => {
    const id = window.setInterval(() => setTip((i) => (i + 1) % TIPS.length), 6000);
    return () => window.clearInterval(id);
  }, []);

  const seconds = Math.floor(elapsedMs / 1000);
  const slow = seconds >= 45;

  return (
    <div className={`stream-progress${compact ? " compact" : ""}`} role="status" aria-live="polite">
      <div className="sp-head">
        <span className="sp-dot" aria-hidden />
        <strong className="sp-label">{progress.label}</strong>
        <span className="spacer" />
        <span className="muted small">{seconds}초</span>
        {onCancel && <button type="button" className="ghost" onClick={onCancel}>취소</button>}
      </div>

      <div className="sp-bar" aria-label={`진행률 ${Math.round(shown)}%`}>
        <div className="sp-fill" style={{ width: `${shown}%` }} />
      </div>

      <ol className="sp-stages">
        {STAGES.map((s) => {
          const st = stageStatus(progress, s.key);
          const extra = s.key === "agents" && progress.agents.length ? ` (${progress.agentsStarted}/${progress.agents.length})` : "";
          return (
            <li key={s.key} className={`sp-stage ${st}`}>
              <span className="sp-icon" aria-hidden>{st === "done" ? "✓" : st === "active" ? "" : "·"}</span>
              {s.label}{extra}
            </li>
          );
        })}
      </ol>

      {!compact && (
        <>
          <ul className="sp-log">
            {progress.log.slice(-4).map((line, i, arr) => (
              <li key={`${progress.steps}-${i}`} className={i === arr.length - 1 ? "latest" : ""}>{line}</li>
            ))}
          </ul>
          <div className="sp-skeleton" aria-hidden>
            <span style={{ width: "38%" }} />
            <span style={{ width: "92%" }} />
            <span style={{ width: "81%" }} />
            <span style={{ width: "64%" }} />
          </div>
        </>
      )}

      <p className="sp-tip muted small">
        <span className="badge">기다리는 동안</span> {TIPS[tip]}
      </p>
      <p className="muted small sp-hint">
        {slow ? "1년 전체 계획처럼 전문가 여러 명이 함께 보는 질문은 1분 가까이 걸릴 수 있어요."
              : "보통 10~30초, 1년 전체 계획은 1분 안팎 걸려요."}
      </p>
    </div>
  );
}
