"""출력 검증(근거 doc_id·필수 근거·금칙어·기간 교정·태그 제거) 테스트 (Bedrock 불필요).

실행: python -m unittest tests.test_output_validation
"""
import asyncio
import unittest

from src import guards, tracing
from src.agent import SajuPlannerApp
from src.schemas import Claim, StructuredAnswer, render_answer, strip_doc_tags

CHART = {
    "reference_date": "2026-10-21", "hour_known": True, "calendar_input": "solar",
    "yongsin_elements": ["火", "水", "木"],
    "months": [{"index": 2, "month_ganzhi": "己亥", "start_date": "2026-11-07", "end_date": "2026-12-06",
                "clash_with_day_branch": False}],
}


def make_state() -> tracing.RequestState:
    st = tracing.start_request()
    st.chart = CHART
    st.add_context("mcp:saju", "[사주 요약] ...", "tool", "saju")
    st.add_context("rule:yongsin:overview", "용신이란\n용신은 ...", "rag", "rule")
    st.add_context("T-OS-KYOTO", "일본 교토 | 방위 동 ...", "tool", "place")
    return st


class TestClaimViolations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = SajuPlannerApp(hub=None, use_llm_guard=False)

    def setUp(self):
        self.st = make_state()
        self.known = set(self.st.contexts)

    def v(self, text, ids):
        return self.app._claim_violations(Claim(section="여행 추천", text=text, doc_ids=ids), self.known)

    def test_grounded_claim_passes(self):
        self.assertEqual(self.v("己亥월(2026-11-07 ~ 2026-12-06)에는 일본 교토를 추천합니다.",
                                ["mcp:saju", "T-OS-KYOTO"]), [])

    def test_unknown_doc_id(self):
        self.assertTrue(any("근거 목록에 없는" in x for x in self.v("좋은 달입니다.", ["rule:nope:x"])))

    def test_ganzhi_requires_mcp_saju(self):
        self.assertTrue(any("mcp:saju" in x for x in self.v("己亥월은 좋은 달입니다.", ["rule:yongsin:overview"])))

    def test_place_requires_its_id(self):
        self.assertTrue(any("T-OS-KYOTO" in x for x in self.v("일본 교토를 추천합니다.", [])))

    def test_fatalism_and_real_estate_words(self):
        self.assertTrue(self.v("내년에 큰 병에 걸립니다.", []))
        self.assertTrue(self.v("이 지역은 시세가 오를 것입니다.", []))

    def test_birth_re_request_flagged_when_chart_exists(self):
        self.assertTrue(any("생년월일" in x for x in self.v("음력 생년월일을 알려주시면 계산해 드리겠습니다.", [])))


class TestRepairAndDrop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = SajuPlannerApp(hub=None, use_llm_guard=False)

    def test_month_range_repaired_from_chart(self):
        make_state()
        ans = StructuredAnswer(claims=[Claim(section="월별 운세", text="己亥월(2026-11-08 ~ 2026-12-07)은 좋습니다.",
                                             doc_ids=["mcp:saju"])])
        self.assertEqual(self.app._repair_month_ranges(ans), 1)
        self.assertIn("己亥월(2026-11-07 ~ 2026-12-06)", ans.claims[0].text)

    def test_violating_claim_removed_after_regeneration(self):
        make_state()
        ans = StructuredAnswer(claims=[
            Claim(section="안내", text="참고용 안내입니다.", doc_ids=[]),
            Claim(section="안내", text="내년에 큰 병에 걸립니다.", doc_ids=[]),
        ])
        out = asyncio.run(self.app.guard_output({"answer": ans, "attempts": 2}))
        self.assertEqual([c.text for c in out["answer"].claims], ["참고용 안내입니다."])

    def test_violation_triggers_regeneration_first(self):
        make_state()
        ans = StructuredAnswer(claims=[Claim(section="안내", text="내년에 큰 병에 걸립니다.", doc_ids=[])])
        out = asyncio.run(self.app.guard_output({"answer": ans, "attempts": 1}))
        self.assertTrue(out["violations"])


class TestDeterministicClaims(unittest.TestCase):
    """계획 평가·여행 표 문장은 LLM 없이 도구 결과로 만든다."""

    @classmethod
    def setUpClass(cls):
        cls.app = SajuPlannerApp(hub=None, use_llm_guard=False)

    def test_plan_claim_maps_month_and_rating(self):
        st = make_state()
        st.route = ["plan_agent"]
        st.add_context("tool:evaluate_plan", "{}", "tool", "plan")
        st.plan_results = [{"rating": "보통", "ganzhi": "己亥", "start_date": "2026-11-07", "end_date": "2026-12-06",
                            "month_yongsin_match": [], "clash_with_day_branch": False, "place_id": "T-OS-KYOTO",
                            "alternatives": [], "basis": "rule:travel:plan-rating",
                            "plan": {"kind": "travel", "month": 11, "place": "교토", "year": 2026}}]
        claims = self.app._plan_claims()
        self.assertEqual(len(claims), 1)
        text = claims[0].text
        self.assertIn("2026년 11월 교토 여행은 己亥월(2026-11-07 ~ 2026-12-06)", text)
        self.assertIn("'보통'", text)
        self.assertEqual(claims[0].doc_ids, ["mcp:saju", "tool:evaluate_plan", "T-OS-KYOTO"])
        self.assertEqual(self.app._claim_violations(claims[0], set(st.contexts)), [])

    def test_plan_out_of_window_keeps_year(self):
        st = make_state()
        st.route = ["plan_agent"]
        st.plan_results = [{"rating": None, "message": "조회 범위 밖이라 평가하지 않습니다.",
                            "plan": {"kind": "move", "month": 3, "place": None, "year": 2028}}]
        self.assertTrue(self.app._plan_claims()[0].text.startswith("2028년 3월 이사는"))

    def test_travel_claims_state_yongsin_basis(self):
        st = make_state()
        st.travel_picks = {2: {"overseas": {"pick": {"id": "T-OS-KYOTO", "name": "일본 교토", "direction": "동",
                                                     "elements": ["木", "土"], "score_breakdown": {"direction": 1}}}}}
        claims = self.app._travel_claims()
        self.assertIn("용신이 火·水·木", claims[0].text)
        self.assertIn("일본 교토(동쪽, 용신 木 기운·용신 방위, 대나무숲·사찰 정원)를 추천합니다", claims[1].text)
        # 코드가 만든 문장도 LLM 문장과 같은 근거 검사를 통과해야 한다
        for c in claims:
            self.assertEqual(self.app._claim_violations(c, set(st.contexts)), [], c.text)

    def test_josa_and_birth_guess(self):
        from src.agent import BIRTH_GUESS_RE, josa
        self.assertEqual((josa("삿포로", "은", "는"), josa("남극", "은", "는"), josa("木", "과", "와")), ("는", "은", "과"))
        self.assertTrue(self.v_guess("양력 12월 생 남성이시며"))
        self.assertFalse(self.v_guess("2027년 생활 리듬을 정돈하세요."))
        self.assertTrue(BIRTH_GUESS_RE.search("1988년생"))

    def test_date_span_split_at_jeolip_day(self):
        from src import tools
        st = make_state()
        st.chart = {**CHART, "months": [
            {"index": 1, "month_ganzhi": "戊戌", "start_date": "2026-10-08", "end_date": "2026-11-06"},
            {"index": 2, "month_ganzhi": "己亥", "start_date": "2026-11-07", "end_date": "2026-12-06"}]}
        spans = tools.split_date_spans("<BIRTH> 남성. 11월 5일부터 10일까지 여행 가면 어느 달 운으로 봐야 해?")
        self.assertEqual([(p["month_ganzhi"], p["from"], p["to"]) for p in spans[0]["parts"]],
                         [("戊戌", "2026-11-05", "2026-11-06"), ("己亥", "2026-11-07", "2026-11-10")])
        text = self.app._span_text(spans[0])
        self.assertIn("절입일(11월 7일)", text)
        self.assertIn("11월 5일~11월 6일은 戊戌월(2026-10-08 ~ 2026-11-06)", text)

    def test_range_with_trailing_words_is_repaired(self):
        make_state()
        ans = StructuredAnswer(claims=[Claim(section="월별 운세", text="己亥월(2026-11-08 ~ 2026-12-07의 후반부)입니다.",
                                             doc_ids=["mcp:saju"])])
        self.assertEqual(self.app._repair_month_ranges(ans), 1)
        self.assertEqual(ans.claims[0].text, "己亥월(2026-11-07 ~ 2026-12-06)입니다.")

    def test_solar_month_summary_dropped_when_table_exists(self):
        from src.agent import SOLAR_MONTH_RE
        self.assertTrue(SOLAR_MONTH_RE.search("8월부터 10월까지 세 달이 좋습니다"))
        self.assertFalse(SOLAR_MONTH_RE.search("乙巳월(2027-05-06 ~ 2027-06-05)"))

    def v_guess(self, text):
        st = make_state()
        return any("출생" in x for x in self.app._claim_violations(Claim(section="사주 요약", text=text, doc_ids=["mcp:saju"]),
                                                                   set(st.contexts)))


class TestNoTagLeak(unittest.TestCase):
    def test_tags_stripped_from_answer_text(self):
        ans = StructuredAnswer(claims=[
            Claim(section="여행 추천", text="일본 교토를 추천합니다 [T-OS-KYOTO] [mcp:saju].", doc_ids=["T-OS-KYOTO"]),
            Claim(section="사주 요약", text="용신은 균형을 잡는 오행입니다 [rule:yongsin:overview]", doc_ids=[]),
        ])
        self.assertEqual(strip_doc_tags(ans), 3)
        text = render_answer(ans, disclaimer=guards.DISCLAIMER)
        for tag in ("[rule:", "[mcp:", "[tool:", "[R-", "[T-"):
            self.assertNotIn(tag, text)
        self.assertEqual(ans.claims[0].doc_ids, ["T-OS-KYOTO"])   # 근거는 doc_ids 에 남는다


if __name__ == "__main__":
    unittest.main()
