# tracing.py - 요청별 trace 와 contexts 수집 (Observability)
"""한 번의 /query 요청 동안 일어난 단계(가드, 파서, 검색, 도구, 에이전트, LLM)를 기록하고
답변 근거가 된 문서(contexts)를 모읍니다.

- 요청마다 새 RequestState 를 만들고 contextvars 로 도구 함수에 전달합니다 (전역 상태 없음)
- 모든 input/output 은 생년월일 패턴을 <BIRTH> 로 바꾼 뒤 300자로 자릅니다
- LangChain 콜백(TraceCallback)이 LLM·도구 호출을, 노드 코드가 가드·파서 단계를 직접 append 합니다
"""
import contextvars
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

MAX_LEN = 300

# 4자리·2자리 연도 날짜, 출생 시각 표현을 가린다 (파서가 놓친 경우까지 대비한 2중 마스킹)
_BIRTH_LIKE = [
    re.compile(r"(?<!\d)(?:19|20)\d{2}\s*년\s*(?:윤\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일"),
    re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}(?!\d)"),
    re.compile(r"(?<!\d)\d{2}\s*년\s*(?:윤\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일"),
    re.compile(r"(?<!\d)\d{2}\s*년\s*생"),
    re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)"),
]


_YEAR = re.compile(r"(?:19|20)\d{2}")


def _mask_match(m: re.Match) -> str:
    """기준일 이후 연도(조회 범위 날짜)는 생년월일이 아니므로 남긴다."""
    from src import config
    y = _YEAR.match(m.group(0))
    if y and int(y.group(0)) >= int(config.REFERENCE_DATE[:4]):
        return m.group(0)
    return "<BIRTH>"


def mask_text(text: str) -> str:
    for pat in _BIRTH_LIKE:
        text = pat.sub(_mask_match, text)
    return text


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = mask_text(text)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN] + "…"


@dataclass
class RequestState:
    """요청 하나의 trace·contexts·사주 요약을 담는다."""
    trace: list[dict] = field(default_factory=list)
    contexts: dict[str, dict] = field(default_factory=dict)   # doc_id → context (입력 순서 유지)
    chart: dict | None = None                                 # 사주 요약 (원본 생년월일 없음)
    travel_picks: dict[int, dict] = field(default_factory=dict)  # 절기 월 번호 → recommend_travel 결과
    season: str | None = None                                 # 질문이 가리킨 계절 (명리 기준)
    plan_results: list[dict] = field(default_factory=list)    # evaluate_plan 결과 (계획 평가 문장은 코드가 만든다)
    route: list[str] = field(default_factory=list)            # 의도 라우터가 고른 필수 에이전트
    supervisor_pick: list[str] = field(default_factory=list)  # Supervisor LLM 이 고른 에이전트 (실행은 동시에)
    date_spans: list[dict] = field(default_factory=list)      # 질문의 날짜 구간을 절기 월로 나눈 결과
    travel_scope: str | None = None                          # 질문(이어 묻기면 이번 말)이 정한 국내/해외 범위
    exclude_ids: list[str] = field(default_factory=list)     # 대화에서 이미 보여 준 장소 (다른 곳 요청 시 제외)
    final_claims: list[dict] = field(default_factory=list)   # 최종 문장 [{section, text, origin}] (대화 탭 필터용)
    final_extra: list[str] = field(default_factory=list)     # 부분 거절 문구
    # 스트리밍 API 의 진행 리스너 listener(kind, data). 없으면(기존 /query·/chat) 아무것도 보내지 않는다
    listener: Callable[[str, dict], None] | None = field(default=None, repr=False)
    residence: dict | None = None                             # recommend_residence 결과 (주거 Top 3·이사 시기 문장은 코드가 만든다)
    started: float = field(default_factory=time.perf_counter)

    def add_step(self, step: str, input: Any = "", output: Any = "", type: str | None = None,
                 name: str | None = None, latency_ms: int | None = None) -> None:
        entry = {"step": step, "input": _clip(input), "output": _clip(output)}
        if type:
            entry["type"] = type
        if name:
            entry["name"] = name
        if latency_ms is not None:
            entry["latency_ms"] = latency_ms
        self.trace.append(entry)
        self._notify("step", entry)

    def emit(self, stage: str, label: str, **extra: Any) -> None:
        """스트리밍 화면용 단계 알림 (trace 에는 남기지 않는다)."""
        self._notify("stage", {"stage": stage, "label": label, **extra})

    def _notify(self, kind: str, data: dict) -> None:
        if self.listener is not None:
            try:
                self.listener(kind, data)
            except Exception:   # 진행 알림 실패가 답변 생성을 막지 않게 한다
                pass

    def add_context(self, doc_id: str, text: str, source: str, doc_type: str) -> None:
        text = mask_text(text)
        if doc_id in self.contexts:
            if text not in self.contexts[doc_id]["text"]:
                self.contexts[doc_id]["text"] += "\n" + text
            return
        self.contexts[doc_id] = {"doc_id": doc_id, "text": text, "source": source, "doc_type": doc_type}

    def context_list(self) -> list[dict]:
        return list(self.contexts.values())

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.trace if t.get("type") in ("tool", "retrieval") and t.get("name")]

    @property
    def total_latency_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


_current: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar("request_state", default=None)


def start_request() -> RequestState:
    state = RequestState()
    _current.set(state)
    return state


def current() -> RequestState:
    state = _current.get()
    if state is None:   # 테스트 등에서 요청 밖에서 도구를 부른 경우
        state = start_request()
    return state


class TraceCallback(BaseCallbackHandler):
    """LLM 호출, 도구 호출, Supervisor 핸드오프를 trace 에 남긴다."""

    def __init__(self, state: RequestState):
        self.state = state
        self._starts: dict[Any, tuple[float, str, str]] = {}

    @staticmethod
    def _node(kwargs) -> str:
        return (kwargs.get("metadata") or {}).get("langgraph_node") or "llm"

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        last = messages[0][-1] if messages and messages[0] else ""
        self._starts[run_id] = (time.perf_counter(), self._node(kwargs), getattr(last, "content", last))

    def on_llm_end(self, response, *, run_id, **kwargs):
        start, node, last = self._starts.pop(run_id, (time.perf_counter(), "llm", ""))
        try:
            msg = response.generations[0][0].message
            calls = [c["name"] for c in getattr(msg, "tool_calls", []) or []]
            out = f"tool_calls={calls}" if calls else msg.content
        except Exception:
            out = ""
        self.state.add_step(f"llm:{node}", last, out, type="llm", name=node,
                            latency_ms=int((time.perf_counter() - start) * 1000))

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = (serialized or {}).get("name", "")
        self._starts[run_id] = (time.perf_counter(), name, input_str)

    def on_tool_end(self, output, *, run_id, **kwargs):
        start, name, input_str = self._starts.pop(run_id, (time.perf_counter(), "", ""))
        latency = int((time.perf_counter() - start) * 1000)
        text = getattr(output, "content", output)
        if name.startswith("transfer_to_"):
            self.state.add_step(f"agent:{name[len('transfer_to_'):]}", "", "handoff", type="agent",
                                name=name[len("transfer_to_"):], latency_ms=latency)
        elif name.startswith("transfer_back"):
            return
        else:
            # 내부 MCP 호출은 도구 함수가 직접 tool 로 기록하므로 여기서는 에이전트용 도구만 남긴다
            self.state.add_step(f"agent_tool:{name}", input_str, text, type="agent_tool", name=name,
                                latency_ms=latency)
