"""POST /query 계약·요청 검증·무상태 테스트 (Bedrock 불필요).

실행: python -m unittest tests.test_api
"""
import asyncio
import unittest

from fastapi.testclient import TestClient

from src import api, guards
from src.agent import SajuPlannerApp

THIRD_PARTY_Q = "남자친구가 1993년 5월 3일생인데 걔 사주랑 내년 운세 봐줘."


class StubApp:
    """그래프 대신 계약 형태만 돌려주는 가짜 앱."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def run_query(self, question: str) -> dict:
        if self.fail:
            raise RuntimeError(f"bedrock down: {question}")
        return {"answer": "ok", "contexts": [{"doc_id": "mcp:saju", "text": "t", "source": "tool", "doc_type": "saju"}],
                "trace": [{"step": "guard:input", "input": "", "output": "allow", "type": "guard", "name": "input_guard"}],
                "total_latency_ms": 1, "reference_date": "2026-10-21"}

    async def close(self):
        pass


def client(app) -> TestClient:
    api.app_state["app"] = app
    return TestClient(api.api)


class TestContract(unittest.TestCase):
    def test_response_keys(self):
        with client(StubApp()) as c:
            body = c.post("/query", json={"question": "용신이 뭐야?"}).json()
        self.assertEqual({"answer", "contexts", "trace"} - set(body), set())
        self.assertEqual({"doc_id", "text"} - set(body["contexts"][0]), set())
        self.assertEqual({"step", "input", "output"} - set(body["trace"][0]), set())

    def test_invalid_requests_get_422(self):
        with client(StubApp()) as c:
            for kwargs in ({"json": {"question": ""}}, {"json": {"question": "   "}},
                           {"json": {"question": "가" * 1001}}, {"json": {}},
                           {"content": "not json", "headers": {"Content-Type": "application/json"}}):
                r = c.post("/query", **kwargs)
                self.assertEqual(r.status_code, 422, kwargs)
                self.assertEqual(r.json()["detail"], api.INVALID_REQUEST_MESSAGE)

    def test_internal_error_does_not_leak_input(self):
        with client(StubApp(fail=True)) as c:
            r = c.post("/query", json={"question": "1992년 4월 12일생 여자"})
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("1992", r.text)


class TestStatelessRefusals(unittest.TestCase):
    """실제 그래프로 거절 경로를 두 번 돌려 상태가 섞이지 않는지 본다 (LLM 호출 없음)."""

    @classmethod
    def setUpClass(cls):
        cls.app = SajuPlannerApp(hub=None, use_llm_guard=False)

    def test_refusal_contract_and_no_carry_over(self):
        a = asyncio.run(self.app.run_query(THIRD_PARTY_Q))
        b = asyncio.run(self.app.run_query(THIRD_PARTY_Q))
        for r in (a, b):
            self.assertEqual(r["answer"], guards.REFUSAL_MESSAGES["third_party"])
            self.assertEqual(r["contexts"], [])
            self.assertEqual(r["trace"][0]["step"], "guard:input")
        self.assertEqual(len(a["trace"]), len(b["trace"]))
        self.assertIsNot(a["trace"], b["trace"])

    def test_clarification_ends_at_parse_step(self):
        r = asyncio.run(self.app.run_query("1991년 2월 30일 양력생 여자예요. 내년 여행지 추천해줘."))
        self.assertEqual(r["contexts"], [])
        self.assertEqual([t["step"] for t in r["trace"]], ["parse:birth", "guard:input"])


if __name__ == "__main__":
    unittest.main()
