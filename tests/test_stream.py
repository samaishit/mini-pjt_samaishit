"""스트리밍 API(/query/stream, /chat/stream) 테스트 (Bedrock 불필요: 가짜 앱).

- 단계 알림(stage)·trace(step)를 실시간으로 보내고, 답변은 끝에 result 로 한 번에 보낸다
- 기존 /query·/chat 응답(trace 포함)은 바뀌지 않는다: 리스너가 없으면 진행 알림을 보내지 않는다
실행: python -m unittest tests.test_stream
"""
import json
import unittest

from fastapi.testclient import TestClient

from src import api, tracing


class StreamApp:
    """run_turn 이 실제 그래프처럼 단계 알림과 trace 를 남기는 가짜 앱."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def run_turn(self, question, preset_chart=None, exclude_ids=None, prior_answer=None, listener=None):
        st = tracing.start_request()
        st.listener = listener
        st.emit("input", "질문과 출생 정보를 확인하고 있어요")
        st.add_step("guard:input", "<BIRTH> 남성", "allow: []", type="guard", name="input_guard")
        st.emit("route", "질문에 맞는 전문가를 골랐어요: 여행", agents=["travel_agent"])
        if self.fail:
            raise RuntimeError(f"bedrock down: {question}")
        st.final_claims = [{"section": "한눈에 보기", "text": "결론입니다.", "origin": "llm"}]
        return {"answer": "### 한눈에 보기\n- 결론입니다.", "contexts": [], "trace": st.trace}, st

    async def run_query(self, question):
        return (await self.run_turn(question))[0]

    async def close(self):
        pass


def events(response) -> list[tuple[str, dict]]:
    out, kind = [], None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            kind = line[len("event: "):]
        elif line.startswith("data: "):
            out.append((kind, json.loads(line[len("data: "):])))
    return out


def client(app) -> TestClient:
    api.app_state["app"] = app
    api.app_state.pop("chat", None)
    return TestClient(api.api)


class TestQueryStream(unittest.TestCase):
    def test_stages_then_steps_then_result(self):
        with client(StreamApp()) as c:
            r = c.post("/query/stream", json={"question": "1988년 11월 3일 오전 7시 양력생 남성. 여행지?"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        ev = events(r)
        kinds = [k for k, _ in ev]
        self.assertEqual(ev[0], ("stage", {"stage": "start", "label": "요청을 받았어요"}))
        self.assertIn(("stage", {"stage": "route", "label": "질문에 맞는 전문가를 골랐어요: 여행", "agents": ["travel_agent"]}), ev)
        self.assertIn("step", kinds)
        self.assertLess(kinds.index("step"), kinds.index("result"))     # trace 는 답보다 먼저 흘러간다
        self.assertEqual(ev[-2], ("stage", {"stage": "done", "label": "답변이 준비됐어요"}))
        self.assertEqual(ev[-1][1]["answer"], "### 한눈에 보기\n- 결론입니다.")
        self.assertNotIn("1988년", r.text)                               # 진행 알림·trace 에 원본 생년월일 없음

    def test_error_event_does_not_leak_input(self):
        with client(StreamApp(fail=True)) as c:
            r = c.post("/query/stream", json={"question": "1992년 4월 12일생 여자"})
        kind, data = events(r)[-1]
        self.assertEqual(kind, "error")
        self.assertEqual(data["error_type"], "RuntimeError")
        self.assertNotIn("1992", r.text)

    def test_invalid_request_is_422(self):
        with client(StreamApp()) as c:
            self.assertEqual(c.post("/query/stream", json={"question": ""}).status_code, 422)
            self.assertEqual(c.post("/chat/stream", json={"message": " "}).status_code, 422)


class TestChatStream(unittest.TestCase):
    def test_chat_stream_returns_session(self):
        with client(StreamApp()) as c:
            r = c.post("/chat/stream", json={"message": "1988년 11월 3일 오전 7시 양력생 남성입니다. 여행지 추천해줘."})
        result = events(r)[-1][1]
        self.assertIn("session_id", result["session"])
        self.assertIn("view", result)


class TestListenerIsOptional(unittest.TestCase):
    def test_no_listener_no_events_same_trace(self):
        st = tracing.start_request()
        st.emit("input", "무시됨")                       # 리스너가 없으면 아무 일도 없다
        st.add_step("guard:input", "a", "b")
        self.assertEqual(st.trace, [{"step": "guard:input", "input": "a", "output": "b"}])   # 단계 알림은 trace 에 남지 않는다

    def test_listener_receives_steps_and_stages(self):
        got = []
        st = tracing.start_request()
        st.listener = lambda kind, data: got.append(kind)
        st.emit("saju", "계산 중")
        st.add_step("tool:calculate_saju", "", "ok")
        self.assertEqual(got, ["stage", "step"])

    def test_broken_listener_does_not_break_request(self):
        st = tracing.start_request()
        st.listener = lambda kind, data: 1 / 0
        st.add_step("guard:input", "a", "b")
        self.assertEqual(len(st.trace), 1)


if __name__ == "__main__":
    unittest.main()
