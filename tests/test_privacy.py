"""개인정보(원본 생년월일) 비노출 테스트 (Bedrock 불필요).

실행: python -m unittest tests.test_privacy
"""
import asyncio
import json
import unittest
from pathlib import Path

from src import config, guards, tools, tracing
from src.agent import SajuPlannerApp
from src.birth_parser import parse
from tests.test_birth_parser import load_queries

# 평가셋에 쓰인 원본 생년월일·시각 표현
RAW = ["1992년 4월 12일", "1988년 11월 3일", "1990년 7월 15일", "1993년 윤3월 10일", "1993년 5월 3일",
       "1991년 2월 30일", "1992-04-12", "1988-11-03", "오후 2시 30분", "오전 7시"]


class TestMasking(unittest.TestCase):
    def test_parser_masks_every_eval_input(self):
        for qid, text in load_queries().items():
            masked = parse(text).masked_text
            for raw in RAW:
                self.assertNotIn(raw, masked, f"#{qid}")

    def test_trace_mask_covers_2digit_and_korean_formats(self):
        for raw in ("1992년 4월 12일", "1992.4.12", "92년생", "92년 4월 12일", "19920412", "1992-04-12"):
            self.assertNotIn(raw, tracing.mask_text(f"생일 {raw}"))

    def test_reference_window_dates_are_not_masked(self):
        self.assertIn("2026-11-07", tracing.mask_text("己亥월(2026-11-07 ~ 2026-12-06)"))


class FakeTool:
    async def ainvoke(self, args):
        return json.dumps({"input": {"year": 1992, "month": 4, "day": 12}, "pillars": {}})


class TestSajuToolRecording(unittest.TestCase):
    def test_private_mcp_call_records_neither_input_nor_output(self):
        hub = tools.MCPHub()
        hub.tools["calculate_saju"] = FakeTool()
        st = tracing.start_request()
        asyncio.run(hub.call("calculate_saju", {"year": 1992, "month": 4, "day": 12}, private=True))
        blob = json.dumps(st.trace, ensure_ascii=False)
        self.assertNotIn("1992", blob)


class TestRefusalPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = SajuPlannerApp(hub=None, use_llm_guard=False)

    def test_no_raw_birth_in_short_circuit_responses(self):
        q = load_queries()
        for qid in (10, 11, 17, 18, 19):
            r = asyncio.run(self.app.run_query(q[qid]))
            blob = json.dumps(r, ensure_ascii=False)
            for raw in RAW:
                self.assertNotIn(raw, blob, f"#{qid}")
        if guards.AUDIT_LOG.exists():
            log = guards.AUDIT_LOG.read_text(encoding="utf-8")
            for raw in RAW:
                self.assertNotIn(raw, log)


class TestCommittedEvalOutputs(unittest.TestCase):
    def test_runs_files_contain_no_raw_birth(self):
        runs = Path(config.PROJECT_ROOT / "evaluation" / "runs")
        files = sorted(runs.glob("round*.json")) if runs.exists() else []
        for f in files:
            text = f.read_text(encoding="utf-8")
            for raw in RAW:
                self.assertNotIn(raw, text, f.name)


if __name__ == "__main__":
    unittest.main()
