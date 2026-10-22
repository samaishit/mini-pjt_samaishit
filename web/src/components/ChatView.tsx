import { useEffect, useRef, useState } from "react";
import { SajuMarkdown } from "./SajuMarkdown";
import { CLIENT_TIMEOUT_MS, endChat, foldedMarkdown, postChat, type ChatResponse, type ChatSession } from "../api";
import { birthSentence, MAX_QUESTION_CHARS, validateBirth, type BirthFields } from "../composer";
import { BirthForm } from "./BirthForm";
import { ResultView } from "./ResultView";
import { StreamProgress } from "./StreamProgress";
import { INITIAL_PROGRESS, postChatStream, reduceProgress, type Progress } from "../stream";

interface UserMsg {
  role: "user";
  text: string; // 사용자가 입력한 문장 (화면에만 있고 저장하지 않는다)
}

interface BotMsg {
  role: "assistant";
  data: ChatResponse;
  status: number;
  elapsedMs: number;
}

interface ErrorMsg {
  role: "error";
  text: string;
}

type Msg = UserMsg | BotMsg | ErrorMsg;

const FOLLOWUP_CHIPS = ["그럼 해외는?", "다른 곳도 알려줘", "그 달에 조심할 점은?", "이사하기 좋은 달은?"];

const DEFAULT_BIRTH: BirthFields = {
  year: 1988, month: 11, day: 3, calendar: "solar", leap: false,
  timeKnown: true, hour: 7, minute: 0, gender: "남",
};

/** 대화 탭: 세션의 단기 기억(사주 요약·앞 질문·보여 준 장소)으로 이어 묻기를 한다. */
export function ChatView() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [session, setSession] = useState<ChatSession | null>(null);
  const [input, setInput] = useState("");
  const [attachBirth, setAttachBirth] = useState(true);
  const [birth, setBirth] = useState<BirthFields>(DEFAULT_BIRTH);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<Progress>(INITIAL_PROGRESS);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const hasChart = !!session?.has_chart;
  // 사주 요약을 아직 기억하지 않았을 때만 출생 정보를 첫 메시지에 붙인다
  const useBirth = attachBirth && !hasChart;
  const birthError = useBirth ? validateBirth(birth) : null;
  const outgoing = (useBirth ? `${birthSentence(birth)} ` : "") + input.trim();
  const canSend = !loading && !!input.trim() && !birthError && outgoing.length <= MAX_QUESTION_CHARS;

  useEffect(() => {
    if (!loading) return;
    const started = performance.now();
    const id = window.setInterval(() => setElapsed(performance.now() - started), 200);
    return () => window.clearInterval(id);
  }, [loading]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  async function send(text?: string) {
    const message = text !== undefined ? text : outgoing;
    if (loading || !message.trim()) return;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const timer = window.setTimeout(() => ctrl.abort(), CLIENT_TIMEOUT_MS);
    setMessages((m) => [...m, { role: "user", text: message }]);
    setInput("");
    setLoading(true);
    setElapsed(0);
    setProgress(INITIAL_PROGRESS);
    const started = performance.now();
    // 스트리밍으로 단계 진행을 받으며 기다린다. 스트리밍을 지원하지 않는 서버면 기존 /chat 으로 다시 요청
    let res = await postChatStream(message, session?.session_id ?? null, (e) => setProgress((p) => reduceProgress(p, e)),
                                   ctrl.signal);
    if (!res.ok && (res.status === 404 || res.status === 405)) res = await postChat(message, session?.session_id ?? null, ctrl.signal);
    window.clearTimeout(timer);
    setLoading(false);
    abortRef.current = null;
    if (res.ok) {
      setSession(res.data.session);
      setMessages((m) => [...m, { role: "assistant", data: res.data, status: res.status, elapsedMs: performance.now() - started }]);
    } else {
      setMessages((m) => [...m, { role: "error", text: res.message }]);
    }
  }

  async function reset() {
    abortRef.current?.abort();
    if (session?.session_id) await endChat(session.session_id);
    setSession(null);
    setMessages([]);
    setInput("");
  }

  return (
    <div className="chat">
      <article className="panel memory">
        <div className="memory-head">
          <strong>대화 기억</strong>
          <span className="spacer" />
          <button type="button" className="ghost" onClick={reset} disabled={!session && !messages.length}>새 대화</button>
        </div>
        {session ? (
          <dl className="memory-list">
            <dt>사주 요약</dt>
            <dd>{session.chart_brief ?? "아직 없음 (첫 메시지에 출생 정보를 적어 주세요)"}</dd>
            <dt>대화</dt>
            <dd>{session.turns ?? 0}턴 · 보여 준 장소 {session.shown_places ?? 0}곳</dd>
            <dt>이어 묻기의 맥락</dt>
            <dd className="small">{session.last_question ?? "없음"}</dd>
          </dl>
        ) : (
          <p className="muted small">대화를 시작하면 이 대화 동안 사주 요약과 앞 질문을 기억해, 다음 질문에서 생년월일을 다시 적지 않아도 됩니다.</p>
        )}
        <p className="muted small note">
          서버는 원본 생년월일을 저장하지 않고, &lt;BIRTH&gt;로 가린 질문과 계산된 사주 요약만 1시간 동안 기억합니다. '새 대화'를 누르면 바로 지웁니다.
        </p>
        {!hasChart && (
          <>
            <label className="check">
              <input type="checkbox" role="switch" checked={attachBirth} onChange={(e) => setAttachBirth(e.target.checked)} />
              첫 메시지에 출생 정보 붙이기
            </label>
            {attachBirth && <BirthForm value={birth} onChange={setBirth} />}
          </>
        )}
      </article>

      <article className="panel thread">
        <div className="messages" aria-live="polite">
          {!messages.length && (
            <div className="placeholder muted">
              <p>예: <em>내년 5월에 여행 어디로 가면 좋을까?</em> → <em>그럼 해외는?</em> → <em>다른 곳도 알려줘</em></p>
              <p className="small">두 번째 질문부터는 출생 정보 없이 이어서 물어보세요.</p>
            </div>
          )}
          {messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="bubble user">{m.text}</div>
            ) : m.role === "error" ? (
              <div key={i} className="bubble error-box" role="alert">{m.text}</div>
            ) : (
              <div key={i} className="bubble bot">
                {m.data.session.followup && <span className="badge chip">이어 묻기</span>}
                <ResultView data={m.data} status={m.status} elapsedMs={m.elapsedMs} />
                {m.data.view && m.data.view.hidden_repeated + m.data.view.hidden_out_of_focus > 0 && (
                  <details className="folded">
                    <summary>
                      접은 내용 펼치기 ·{" "}
                      {[m.data.view.hidden_repeated ? `앞에서 본 내용 ${m.data.view.hidden_repeated}건` : "",
                        m.data.view.hidden_out_of_focus ? `다른 섹션 ${m.data.view.hidden_out_of_focus}건` : ""]
                        .filter(Boolean).join(", ")}
                    </summary>
                    <div className="answer folded-body"><SajuMarkdown>{foldedMarkdown(m.data.view)}</SajuMarkdown></div>
                  </details>
                )}
              </div>
            ),
          )}
          {loading && (
            <div className="bubble bot">
              <StreamProgress progress={progress} elapsedMs={elapsed} onCancel={() => abortRef.current?.abort()} />
            </div>
          )}
          <div ref={endRef} />
        </div>

        {session?.has_chart && (
          <div className="chips">
            {FOLLOWUP_CHIPS.map((c) => (
              <button key={c} type="button" className="ghost chip-btn" disabled={loading} onClick={() => send(c)}>{c}</button>
            ))}
          </div>
        )}

        {birthError && <p className="warn">{birthError}</p>}
        <div className="composer">
          <textarea rows={2} value={input} placeholder={hasChart ? "이어서 물어보세요" : "질문을 입력하세요"}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault();
                        if (canSend) send();
                      }
                    }} />
          <button type="button" disabled={!canSend} onClick={() => send()} aria-busy={loading}>보내기</button>
        </div>
        {useBirth && input.trim() && <p className="muted small">보낼 문장: {outgoing}</p>}
      </article>
    </div>
  );
}
