import { useEffect, useMemo, useRef, useState } from "react";
import { CLIENT_TIMEOUT_MS, getExamples, postQuery, type Example, type QueryResponse } from "./api";
import { composeQuestion, MAX_QUESTION_CHARS, validateBirth, type BirthFields } from "./composer";
import { BirthForm } from "./components/BirthForm";
import { ChatView } from "./components/ChatView";
import { ResultView } from "./components/ResultView";
import { StreamProgress } from "./components/StreamProgress";
import { ThemeToggle } from "./components/ThemeToggle";
import { INITIAL_PROGRESS, postQueryStream, reduceProgress, type Progress } from "./stream";

const DEFAULT_BIRTH: BirthFields = {
  year: 1988, month: 11, day: 3, calendar: "solar", leap: false,
  timeKnown: true, hour: 7, minute: 0, gender: "남",
};

const CATEGORY_LABELS: Record<Example["category"], string> = {
  positive: "정상", negative: "정보 없음·잘못된 입력", edge: "경계", guardrail: "가드레일",
};

type Mode = "form" | "free";

interface Done {
  data: QueryResponse;
  status: number;
  elapsedMs: number;
}

export default function App() {
  // ?view=chat 으로 열면 대화 탭부터 보여 준다 (링크 공유용)
  const [view, setView] = useState<"single" | "chat">(
    () => (new URLSearchParams(window.location.search).get("view") === "chat" ? "chat" : "single"));
  const [mode, setMode] = useState<Mode>("form");
  const [birth, setBirth] = useState<BirthFields>(DEFAULT_BIRTH);
  const [question, setQuestion] = useState("내년 5월에 여행 어디로 가면 좋을까?");
  const [freeText, setFreeText] = useState("");
  const [examples, setExamples] = useState<Example[]>([]);
  const [referenceDate, setReferenceDate] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<Done | null>(null);
  const [progress, setProgress] = useState<Progress>(INITIAL_PROGRESS);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    getExamples().then((r) => {
      if (r) {
        setExamples(r.examples);
        setReferenceDate(r.reference_date);
      }
    });
  }, []);

  // 대기 중 경과 시간 표시 (응답은 보통 15~80초)
  useEffect(() => {
    if (!loading) return;
    const started = performance.now();
    const id = window.setInterval(() => setElapsed(performance.now() - started), 200);
    return () => window.clearInterval(id);
  }, [loading]);

  const birthError = mode === "form" ? validateBirth(birth) : null;
  const finalQuestion = useMemo(
    () => (mode === "form" ? composeQuestion(birth, question) : freeText.trim()),
    [mode, birth, question, freeText],
  );
  const tooLong = finalQuestion.length > MAX_QUESTION_CHARS;
  const emptyQuestion = mode === "form" ? !question.trim() : !freeText.trim();
  const canSend = !loading && !birthError && !tooLong && !emptyQuestion;

  async function send() {
    if (!canSend) return;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const timer = window.setTimeout(() => ctrl.abort(), CLIENT_TIMEOUT_MS);
    setLoading(true);
    setElapsed(0);
    setError(null);
    setDone(null);
    setProgress(INITIAL_PROGRESS);
    const started = performance.now();
    // 스트리밍 API 로 단계 진행을 받으며 기다린다. 스트리밍을 지원하지 않는 서버면 기존 /query 로 다시 요청
    let res = await postQueryStream(finalQuestion, (e) => setProgress((p) => reduceProgress(p, e)), ctrl.signal);
    if (!res.ok && (res.status === 404 || res.status === 405)) res = await postQuery(finalQuestion, ctrl.signal);
    window.clearTimeout(timer);
    setLoading(false);
    abortRef.current = null;
    if (res.ok) setDone({ data: res.data, status: res.status, elapsedMs: performance.now() - started });
    else setError(res.message);
  }

  function pickExample(id: string) {
    const ex = examples.find((e) => String(e.id) === id);
    if (!ex) return;
    setMode("free");
    setFreeText(ex.input);
  }

  return (
    <div className="page">
      <div className="brand-band" aria-hidden title="오방색: 목·화·토·금·수" />
      <header className="top">
        <div className="seal" aria-hidden><span>四</span><span>柱</span><span>生</span><span>活</span></div>
        <div className="brand">
          <h1>사주 라이프 플래너</h1>
          <p>
            타고난 사주와 앞으로 12개월의 흐름으로 여행지·살 곳·내 계획을 근거와 함께 짚어 드립니다.
            {referenceDate && <> 기준일 <strong>{referenceDate}</strong></>}
          </p>
        </div>
        <ThemeToggle />
      </header>

      <nav className="view-tabs" role="tablist" aria-label="화면">
        <button role="tab" aria-selected={view === "single"} className={view === "single" ? "on" : ""}
                onClick={() => setView("single")}>단일 질의 <span className="muted small">POST /query</span></button>
        <button role="tab" aria-selected={view === "chat"} className={view === "chat" ? "on" : ""}
                onClick={() => setView("chat")}>대화 <span className="muted small">POST /chat · 단기 기억</span></button>
      </nav>

      {/* 두 화면을 모두 띄워 두고 표시만 바꾼다 (탭을 바꿔도 입력·대화가 남도록) */}
      <div hidden={view !== "chat"}>
        <ChatView />
      </div>

      <main className="layout" hidden={view !== "single"}>
        <article className="panel input-panel">
          <div className="seg mode" role="tablist" aria-label="입력 방식">
            <button role="tab" aria-selected={mode === "form"} className={mode === "form" ? "on" : ""}
                    onClick={() => setMode("form")}>폼 입력</button>
            <button role="tab" aria-selected={mode === "free"} className={mode === "free" ? "on" : ""}
                    onClick={() => setMode("free")}>직접 입력</button>
          </div>

          {mode === "form" ? (
            <>
              <BirthForm value={birth} onChange={setBirth} />
              <label className="block">
                질문
                <textarea rows={3} value={question} onChange={(e) => setQuestion(e.target.value)}
                          placeholder="예: 앞으로 1년 계획 짜줘 / 12월에 삿포로 여행 괜찮을까?" />
              </label>
              <div className="preview-box">
                <span className="muted small">보낼 문장</span>
                <p>{finalQuestion}</p>
              </div>
            </>
          ) : (
            <label className="block">
              질문 (출생 정보를 문장에 직접 적습니다)
              <textarea rows={7} value={freeText} onChange={(e) => setFreeText(e.target.value)}
                        placeholder="예: 1992년 4월 12일 오후 2시 30분 양력 출생 여성입니다. 내 용신이 뭐야?" />
            </label>
          )}

          {examples.length > 0 && (
            <label className="block">
              예시 질문 (평가셋 24건)
              <select value="" onChange={(e) => pickExample(e.target.value)}>
                <option value="">선택하면 직접 입력란에 넣습니다…</option>
                {(Object.keys(CATEGORY_LABELS) as Example["category"][]).map((cat) => (
                  <optgroup key={cat} label={CATEGORY_LABELS[cat]}>
                    {examples.filter((e) => e.category === cat).map((e) => (
                      <option key={e.id} value={e.id}>#{e.id} {e.input.length > 60 ? `${e.input.slice(0, 60)}…` : e.input}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
          )}

          {birthError && <p className="warn">{birthError}</p>}
          {tooLong && <p className="warn">질문은 {MAX_QUESTION_CHARS}자 이하여야 합니다 ({finalQuestion.length}자).</p>}

          <div className="actions">
            <span className="muted small">{finalQuestion.length} / {MAX_QUESTION_CHARS}</span>
            <span className="spacer" />
            {loading && <button type="button" className="ghost" onClick={() => abortRef.current?.abort()}>취소</button>}
            <button type="button" disabled={!canSend} onClick={send} aria-busy={loading}>
              {loading ? "계산 중…" : "보내기"}
            </button>
          </div>
          <p className="muted small note">
            출생 정보는 이 요청에만 쓰이고 브라우저에 저장하지 않습니다. 서버는 생년월일을 &lt;BIRTH&gt;로 가린 뒤 LLM 에 보냅니다.
          </p>
        </article>

        <article className="panel output-panel">
          {loading && (
            <StreamProgress progress={progress} elapsedMs={elapsed} onCancel={() => abortRef.current?.abort()} />
          )}
          {error && <div className="error-box" role="alert">{error}</div>}
          {done && !loading && <ResultView data={done.data} status={done.status} elapsedMs={done.elapsedMs} />}
          {!loading && !error && !done && (
            <div className="placeholder">
              <h3>이렇게 물어보세요</h3>
              <ol>
                <li>왼쪽에 생년월일·양력/음력·태어난 시각·성별을 넣습니다.</li>
                <li>"내년 5월 여행지?", "1년 계획 짜줘", "12월 삿포로 여행 괜찮을까?"처럼 묻습니다.</li>
                <li><strong>보내기</strong>를 누르면 사주를 계산해 근거와 함께 답합니다.</li>
              </ol>
              <p className="muted small">
                답변 아래 <em>처리 과정</em> 탭에서 질문 확인 → 사주 계산 → 전문가 분석 → 근거 검증 단계를, <em>근거</em> 탭에서 인용한 문서를 볼 수 있습니다.
              </p>
            </div>
          )}
        </article>
      </main>
    </div>
  );
}
