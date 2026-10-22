"""웹 화면 '대화' 탭(POST /chat)의 단기 기억 테스트 (Bedrock 불필요: 가짜 앱).

- 원본 생년월일은 기억에 남지 않는다 (가린 질문·사주 요약만)
- 이어 묻기는 앞 질문(가린 문장)과 합쳐 보내고, 기억한 사주 요약을 다시 쓴다
- '다른 곳'이면 이미 보여 준 장소를 빼고, 세션끼리 섞이지 않는다
실행: python -m unittest tests.test_chat
"""
import asyncio
import json
import time
import unittest
from dataclasses import asdict
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src import api, tools
from src.chat import ChatMemory, ChatService, compose_followup

RAW = ["1988년 11월 3일", "오전 7시", "1992년 4월 12일"]
CHART = {"day_stem": "壬", "yongsin": ["戊", "丙"], "yongsin_elements": ["土", "火"], "pillars": {"day": "壬戌"}}


class FakeApp:
    """run_turn 호출을 기록하고, 질문에 따라 정해진 결과와 요청 상태를 돌려준다."""

    def __init__(self):
        self.calls = []

    async def run_turn(self, question, preset_chart=None, exclude_ids=None, prior_answer=None):
        self.calls.append({"question": question, "preset": preset_chart, "exclude": list(exclude_ids or []),
                           "prior": prior_answer})
        refused = "남자친구" in question
        trace = ([{"step": "guard:input", "input": "", "output": "block: third_party"}] if refused else
                 [{"step": "parse:birth", "input": "", "output": "birth=ok, clarification=None"},
                  {"step": "guard:input", "input": "", "output": "allow: []"}])
        has_birth = "1988년" in question or "1992년" in question
        n = len(self.calls)
        final = [] if refused else [
            {"section": "한눈에 보기", "text": f"결론 {n}", "origin": "llm"},
            {"section": "사주 요약", "text": "원국(타고난 사주 바탕)은 …입니다.", "origin": "code"},   # 턴마다 같은 문장
            {"section": "여행 추천", "text": f"을사(乙巳)월에는 국내 여행지 {n}을 추천합니다.", "origin": "code"},
            {"section": "이사 시기", "text": "이사는 기해(己亥)월에 하면 좋습니다.", "origin": "code"},
            {"section": "용어 풀이", "text": "용신: 나에게 필요한 기운입니다.", "origin": "code"},
        ]
        st = SimpleNamespace(chart=None if refused else (CHART if has_birth or preset_chart else None),
                             travel_picks={} if refused else {1: {"domestic": {"pick": {"id": f"T-KR-{n}"}}}},
                             residence=None, plan_results=[], final_claims=final, final_extra=[],
                             route=["travel_agent", "residence_agent"])
        answer = "죄송합니다." if refused else "### 한눈에 보기\n- 답변입니다."
        return {"answer": answer, "contexts": [], "trace": trace}, st

    async def close(self):
        pass


def run(svc, sid, msg):
    return asyncio.run(svc.turn(sid, msg))


class TestChatMemory(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.svc = ChatService(self.app)

    def test_first_turn_remembers_chart_but_not_raw_birth(self):
        r = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지 추천해줘.")
        s = self.svc.memory.sessions[r["session"]["session_id"]]
        self.assertEqual(s.chart, CHART)
        dump = json.dumps(asdict(s), ensure_ascii=False)
        for raw in RAW:
            self.assertNotIn(raw, dump)
        self.assertIn("<BIRTH>", s.last_question)
        self.assertEqual(r["session"]["chart_brief"], "일간 壬, 용신 戊·丙(土·火)")

    def test_followup_reuses_chart_and_previous_question(self):
        sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지 추천해줘.")["session"]["session_id"]
        r = run(self.svc, sid, "그럼 해외는?")
        call = self.app.calls[-1]
        self.assertTrue(r["session"]["followup"])
        self.assertEqual(call["preset"], CHART)
        self.assertTrue(call["question"].endswith("(이어서 묻는 말) 그럼 해외는?"))
        self.assertIn("내년 5월 여행지", call["question"])
        for raw in RAW:
            self.assertNotIn(raw, call["question"])   # 앞 질문은 가린 문장으로만 다시 보낸다

    def test_other_places_excludes_shown(self):
        sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지 추천해줘.")["session"]["session_id"]
        run(self.svc, sid, "다른 곳도 알려줘")
        self.assertEqual(self.app.calls[-1]["exclude"], ["T-KR-1"])

    def test_new_question_is_not_followup(self):
        sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지 추천해줘.")["session"]["session_id"]
        r = run(self.svc, sid, "내 용신이 뭐고 무슨 의미야?")
        self.assertFalse(r["session"]["followup"])
        self.assertEqual(self.app.calls[-1]["question"], "내 용신이 뭐고 무슨 의미야?")
        self.assertEqual(self.app.calls[-1]["preset"], CHART)   # 사주 요약은 계속 기억

    def test_new_birth_replaces_memory(self):
        sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 여행지 추천해줘.")["session"]["session_id"]
        run(self.svc, sid, "1992년 4월 12일 오후 2시 30분 양력 출생 여성입니다. 내년 여행지 추천해줘.")
        self.assertIsNone(self.app.calls[-1]["preset"])   # 새 생년월일이면 새로 계산

    def test_refusal_does_not_become_context(self):
        sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 여행지 추천해줘.")["session"]["session_id"]
        before = self.svc.memory.sessions[sid].last_question
        r = run(self.svc, sid, "남자친구가 1993년 5월 3일생인데 걔 사주 봐줘.")
        self.assertEqual(r["session"]["kind"], "refusal")
        self.assertEqual(self.svc.memory.sessions[sid].last_question, before)
        self.assertNotIn("1993년 5월 3일", json.dumps(asdict(self.svc.memory.sessions[sid]), ensure_ascii=False))

    def test_sessions_are_isolated_and_droppable(self):
        a = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 여행지 추천해줘.")["session"]["session_id"]
        b = run(self.svc, None, "그럼 해외는?")["session"]["session_id"]   # 새 세션의 첫 말은 이어 묻기가 아니다
        self.assertNotEqual(a, b)
        self.assertIsNone(self.app.calls[-1]["preset"])
        self.assertTrue(self.svc.memory.drop(a))
        self.assertNotIn(a, self.svc.memory.sessions)

    def test_ttl_and_capacity(self):
        mem = ChatMemory(ttl_s=10, max_sessions=2)
        s1, s2, s3 = mem.get(None), mem.get(None), mem.get(None)
        self.assertEqual(len(mem.sessions), 2)
        self.assertNotIn(s1.id, mem.sessions)        # 가장 오래 안 쓴 세션부터 정리
        mem.sessions[s3.id].updated = time.monotonic() - 11
        mem.get(s2.id)
        self.assertNotIn(s3.id, mem.sessions)        # 유효 시간이 지나면 정리

    def test_followup_marker_prefers_latest_months(self):
        q = compose_followup("<BIRTH> 남성. 내년 5월 여행지?", "6월은 어때?")
        self.assertEqual(tools.latest_part(q), " 6월은 어때?")

    def test_scope_follows_latest_statement_and_context_is_capped(self):
        q1 = "<BIRTH> 남성. 내년 5월 국내 여행?"
        q2 = compose_followup(q1, "그럼 해외는?")
        q3 = compose_followup(q2, "다른 곳도")
        q4 = compose_followup(q3, "6월은?")
        q5 = compose_followup(q4, "국내는?")
        self.assertEqual([tools.travel_scope(q) for q in (q1, q2, q3, q4, q5)],
                         ["domestic", "overseas", "overseas", "overseas", "domestic"])
        self.assertTrue(q5.startswith(q1))                      # 첫 질문은 늘 남는다
        self.assertEqual(q5.count("(이어서 묻는 말)"), 3)        # 이어 묻기는 최근 3개까지만


class TestAnswerFilter(unittest.TestCase):
    """대화 탭 답변 필터: 이미 보여 준 문장은 접고, 이어 묻기면 이번 말의 범위만 펼친다."""

    def setUp(self):
        self.app = FakeApp()
        self.svc = ChatService(self.app)
        self.sid = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지와 이사 시기 알려줘.")["session"]["session_id"]

    def shown(self, r):
        return [(sec["name"], c["text"]) for sec in r["view"]["sections"] for c in sec["claims"] if c["shown"]]

    def test_first_turn_shows_everything(self):
        r = run(self.svc, None, "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 5월 여행지와 이사 시기 알려줘.")
        self.assertEqual(r["view"]["mode"], "full")
        self.assertEqual(len(self.shown(r)), 5)
        self.assertEqual(r["view"]["hidden_repeated"] + r["view"]["hidden_out_of_focus"], 0)

    def test_followup_folds_repeated_and_out_of_scope(self):
        r = run(self.svc, self.sid, "그럼 해외는?")
        shown = self.shown(r)
        self.assertIn(("한눈에 보기", "결론 2"), shown)                       # 이번 결론은 늘
        self.assertIn(("여행 추천", "을사(乙巳)월에는 국내 여행지 2을 추천합니다."), shown)   # 새 추천
        self.assertNotIn("사주 요약", [s for s, _ in shown])                 # 같은 문장 → 접힘
        self.assertNotIn("용어 풀이", [s for s, _ in shown])                 # 같은 풀이 → 접힘
        self.assertEqual(r["view"]["hidden_repeated"], 3)                    # 사주 요약·이사 시기·용어 풀이
        self.assertIn("앞 답변과 같은 내용 3건", r["answer"])
        self.assertNotIn("원국", r["answer"])
        self.assertIn("원국", json.dumps(r["view"], ensure_ascii=False))     # 접은 문장도 view 에 남아 펼쳐 볼 수 있다
        self.assertEqual(r["answer_full"], "### 한눈에 보기\n- 답변입니다.")  # 그래프의 원래 답도 그대로 돌려준다

    def test_explicit_intent_narrows_scope(self):
        run(self.svc, self.sid, "그럼 해외는?")
        r = run(self.svc, self.sid, "이사하기 좋은 달은?")
        focus = set(r["view"]["focus"])
        self.assertTrue({"주거 추천", "이사 시기"} <= focus)                  # '이사' → 주거 섹션 ('좋은 달'은 사주 해석도 포함)
        self.assertNotIn("여행 추천", focus)
        self.assertNotIn("여행 추천", [s for s, _ in self.shown(r)])          # 범위 밖 → 접힘
        self.assertIn(("이사 시기", "이사는 기해(己亥)월에 하면 좋습니다."), self.shown(r))   # 범위 안이면 앞서 봤어도 보여 준다

    def test_full_view_keyword_disables_filter(self):
        r = run(self.svc, self.sid, "다시 전체 정리해줘")
        self.assertEqual(r["view"]["mode"], "full")
        self.assertEqual(len(self.shown(r)), 5)

    def test_prior_answer_digest_passed_on_followup(self):
        run(self.svc, self.sid, "그럼 해외는?")
        prior = self.app.calls[-1]["prior"]
        self.assertIn("[한눈에 보기] 결론 1", prior)
        self.assertIn("[여행 추천]", prior)
        self.assertNotIn("1988년", prior)
        self.assertIsNone(self.app.calls[0]["prior"])                         # 첫 턴에는 없음


class TestChatApi(unittest.TestCase):
    def setUp(self):
        api.app_state["app"] = FakeApp()
        api.app_state.pop("chat", None)

    def test_chat_round_trip_and_delete(self):
        with TestClient(api.api) as c:
            r1 = c.post("/chat", json={"message": "1988년 11월 3일 오전 7시 양력생 남성입니다. 내년 여행지 추천해줘."}).json()
            sid = r1["session"]["session_id"]
            r2 = c.post("/chat", json={"message": "그럼 해외는?", "session_id": sid}).json()
            self.assertTrue(r2["session"]["followup"])
            self.assertEqual(r2["session"]["turns"], 2)
            self.assertTrue(c.delete(f"/chat/{sid}").json()["deleted"])
            self.assertFalse(c.post("/chat", json={"message": "그럼 해외는?", "session_id": sid}).json()["session"]["followup"])

    def test_chat_validation(self):
        with TestClient(api.api) as c:
            for body in ({"message": ""}, {"message": "가" * 1001}, {}):
                self.assertEqual(c.post("/chat", json=body).status_code, 422)


if __name__ == "__main__":
    unittest.main()
