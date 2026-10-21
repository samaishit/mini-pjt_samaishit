"""출생 정보 파서 단위 테스트 (Bedrock 불필요).

실행: python -m unittest discover -s tests -v
"""
import csv
import unittest
from pathlib import Path

from src.birth_parser import PLACEHOLDER, parse

ROOT = Path(__file__).resolve().parent.parent
REF = "2026-10-21"


def load_queries() -> dict[int, str]:
    with open(ROOT / "evaluation" / "test_queries.csv", encoding="utf-8") as f:
        return {int(r["id"]): r["input"] for r in csv.DictReader(f)}


class TestEvaluationInputs(unittest.TestCase):
    """평가셋 입력이 기대대로 파싱되는지 확인한다."""

    @classmethod
    def setUpClass(cls):
        cls.q = load_queries()

    def assertBirth(self, qid, y, m, d, calendar, leap, gender, hour, minute):
        r = parse(self.q[qid], REF)
        self.assertIsNone(r.clarification, f"#{qid} 되묻기: {r.clarification}")
        b = r.birth
        self.assertEqual((b.year, b.month, b.day, b.calendar, b.leap, b.gender, b.hour, b.minute),
                         (y, m, d, calendar, leap, gender, hour, minute), f"#{qid}")
        self.assertIn(PLACEHOLDER, r.masked_text)
        return r

    def test_profile_a_solar_with_time(self):
        for qid in (1, 3, 6, 8, 12, 15, 18, 24):
            self.assertBirth(qid, 1992, 4, 12, "solar", False, "여", 14, 30)

    def test_profile_b_solar_with_time(self):
        for qid in (2, 4, 7, 9, 14, 16, 23):
            self.assertBirth(qid, 1988, 11, 3, "solar", False, "남", 7, 0)

    def test_lunar_without_time(self):
        r = self.assertBirth(5, 1990, 7, 15, "lunar", False, "여", None, None)
        self.assertFalse(r.birth.hour_known)
        self.assertIn("hour_unknown", r.notes)
        self.assertEqual(r.birth.to_mcp_args()["hour"], 12)

    def test_lunar_leap_month(self):
        self.assertBirth(13, 1993, 3, 10, "lunar", True, "남", 9, 0)

    def test_invalid_date_feb_30(self):
        self.assertEqual(parse(self.q[10], REF).clarification, "invalid_date")

    def test_missing_gender(self):
        self.assertEqual(parse(self.q[11], REF).clarification, "gender_missing")

    def test_no_birth_data(self):
        for qid in (17, 20, 21, 22):
            r = parse(self.q[qid], REF)
            self.assertIsNone(r.birth)
            self.assertIsNone(r.clarification)
            self.assertEqual(r.masked_text, self.q[qid])

    def test_raw_birth_never_in_masked_text(self):
        for qid, text in self.q.items():
            masked = parse(text, REF).masked_text
            for raw in ("1992년 4월 12일", "1988년 11월 3일", "1990년 7월 15일", "1993년 윤3월 10일",
                        "1993년 5월 3일", "오후 2시 30분", "오전 7시"):
                self.assertNotIn(raw, masked, f"#{qid} 마스킹 누락: {raw}")

    def test_plan_dates_are_not_birth(self):
        # 14번: 둘째 문장의 '11월 5일부터 10일'은 생년월일이 아니고 출생 시각도 아니다
        r = parse(self.q[14], REF)
        self.assertIn("11월 5일부터 10일까지", r.masked_text)


class TestClarifications(unittest.TestCase):
    def test_two_digit_year(self):
        self.assertEqual(parse("92년생 여자인데 내년 여행지 알려줘", REF).clarification, "two_digit_year")
        r = parse("92년 4월 12일생 여자예요", REF)
        self.assertEqual(r.clarification, "two_digit_year")
        self.assertNotIn("92년 4월 12일", r.masked_text)

    def test_calendar_unknown(self):
        self.assertEqual(parse("1992년 4월 12일생 여자예요. 여행지 추천해줘", REF).clarification, "calendar_unknown")

    def test_calendar_conflict(self):
        self.assertEqual(parse("1992년 4월 12일 양력 음력 출생 여자", REF).clarification, "calendar_unknown")

    def test_bare_hour_is_ambiguous(self):
        self.assertEqual(parse("1992년 4월 12일 2시 양력생 여자", REF).clarification, "ambiguous_hour")

    def test_24h_and_colon_times(self):
        self.assertEqual(parse("1992년 4월 12일 14시 양력생 여자", REF).birth.hour, 14)
        b = parse("1992-04-12 14:30 양력 출생 여성", REF).birth
        self.assertEqual((b.year, b.month, b.day, b.hour, b.minute), (1992, 4, 12, 14, 30))

    def test_dotted_date_not_split(self):
        b = parse("1992.4.12. 오후 2시 양력생 여자예요.", REF).birth
        self.assertEqual((b.year, b.month, b.day, b.hour), (1992, 4, 12, 14))

    def test_future_date(self):
        self.assertEqual(parse("2027년 1월 1일 양력생 남자", REF).clarification, "future_date")

    def test_multiple_birth_dates(self):
        self.assertEqual(parse("1992년 4월 12일생이고 1990년 1월 1일생이기도 한 양력 남자", REF).clarification,
                         "multiple_birth_dates")

    def test_gender_words_inside_other_words(self):
        # '남자친구', '여행', '남극'의 글자를 성별로 잡지 않는다
        r = parse("1992년 4월 12일 양력생인데 남자친구랑 남극 여행 가고 싶어", REF)
        self.assertEqual(r.clarification, "gender_missing")

    def test_gender_conflict(self):
        self.assertEqual(parse("1992년 4월 12일 양력생 남자 여자", REF).clarification, "gender_missing")

    def test_year_only_plan_date_ignored(self):
        r = parse("1988년 11월 3일 오전 7시 양력생 남성. 2027년 3월에 이사해도 돼?", REF)
        self.assertEqual(r.birth.year, 1988)
        self.assertIn("2027년 3월", r.masked_text)


if __name__ == "__main__":
    unittest.main()
