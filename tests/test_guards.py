"""입력·출력 가드와 의도 라우팅 단위 테스트 (Bedrock 불필요, LLM 판별은 끈다).

실행: python -m unittest discover -s tests -t . -v
"""
import unittest

from src import guards
from src.agent import required_agents
from src.birth_parser import parse
from src.tracing import mask_text
from tests.test_birth_parser import load_queries


def decide(text: str) -> tuple[str, str | None]:
    if guards.check_third_party(text):
        return "block", "third_party"
    p = parse(text)
    d = guards.input_guard(p.masked_text, p.birth is not None, use_llm=False)
    return d.action, d.category or (",".join(d.partial_categories) or None)


class TestInputGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = load_queries()

    def test_guardrail_rows_blocked(self):
        expected = {17: "injection", 18: "injection", 19: "third_party", 20: "off_topic",
                    21: "real_estate", 22: "explicit"}
        for qid, cat in expected.items():
            self.assertEqual(decide(self.q[qid]), ("block", cat), f"#{qid}")

    def test_normal_rows_allowed(self):
        for qid in (1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 23, 24):
            self.assertEqual(decide(self.q[qid])[0], "allow", f"#{qid}")

    def test_mixed_request_is_partial(self):
        self.assertEqual(decide(self.q[16]), ("partial", "real_estate"))
        self.assertEqual(decide("1988년 11월 3일 양력생 남성인데 여행지 추천해주고 파이썬 코드도 짜줘")[0], "partial")

    def test_companion_mention_is_not_third_party(self):
        self.assertFalse(guards.check_third_party(self.q[15]))
        self.assertFalse(guards.check_third_party("엄마랑 같이 갈 여행지 추천해줘"))

    def test_third_party_variants(self):
        for text in ("엄마 사주 봐줘", "우리 딸 2001년 3월 2일생 운세 알려줘", "친구의 용신이 뭐야?"):
            self.assertTrue(guards.check_third_party(text), text)


class TestOutputGuard(unittest.TestCase):
    def test_fatalism_and_real_estate_detected(self):
        self.assertTrue(guards.output_violations("내년에 큰 병에 걸립니다."))
        self.assertTrue(guards.output_violations("이 지역 아파트는 시세가 오를 것입니다."))

    def test_fixed_phrases_are_ignored(self):
        text = "강동구를 추천합니다.\n" + guards.REFUSAL_MESSAGES["real_estate"] + "\n" + guards.DISCLAIMER
        self.assertEqual(guards.output_violations(text), [])

    def test_reframed_answer_passes(self):
        self.assertEqual(guards.output_violations("건강에 무리하지 않도록 주의하시고 필요하면 전문가와 상담하세요."), [])


class TestTraceMasking(unittest.TestCase):
    def test_mask_text(self):
        for raw in ("1992년 4월 12일", "1992-04-12", "1992.4.12", "92년생", "19920412"):
            self.assertNotIn(raw, mask_text(f"생일은 {raw} 입니다"))


class TestRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = load_queries()

    def test_expected_routes(self):
        expected = {1: ["saju_agent", "travel_agent", "residence_agent"], 2: ["travel_agent"],
                    3: ["residence_agent"], 4: ["plan_agent"], 6: ["saju_agent"], 9: ["plan_agent"],
                    12: ["plan_agent"], 14: ["saju_agent"], 15: ["travel_agent"]}
        for qid, agents in expected.items():
            self.assertEqual(required_agents(parse(self.q[qid]).masked_text), agents, f"#{qid}")


if __name__ == "__main__":
    unittest.main()
