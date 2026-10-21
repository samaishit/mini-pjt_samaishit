"""기준일 고정과 12개 절기 월 범위 테스트 (Bedrock 불필요, 사주 MCP 는 Node 로 로컬 실행).

실행: python -m unittest tests.test_reference_date
"""
import asyncio
import os
import shutil
import subprocess
import sys
import unittest

from src import config, tools, tracing
from src.birth_parser import parse

ROOT = config.PROJECT_ROOT
PROFILE = "1992년 4월 12일 오후 2시 30분 양력 출생 여성"


def reference_date_with_env(value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SAJU_REFERENCE_DATE"}
    if value:
        env["SAJU_REFERENCE_DATE"] = value
    out = subprocess.run([sys.executable, "-c", "from src import config; print(config.REFERENCE_DATE)"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


class TestConfig(unittest.TestCase):
    def test_default_is_pinned_not_system_date(self):
        # .env 에 값이 없다는 전제에서 미설정이면 고정값 2026-10-21
        self.assertEqual(reference_date_with_env(None), "2026-10-21")

    def test_env_override(self):
        self.assertEqual(reference_date_with_env("2027-01-15"), "2027-01-15")


@unittest.skipUnless(shutil.which("node") and (ROOT / "mcp_servers/saju/dist/index.js").exists(),
                     "Node 또는 빌드된 사주 MCP 가 없음")
class TestLuckWindow(unittest.TestCase):
    def window(self, ref: str) -> list[dict]:
        async def run():
            hub = await tools.MCPHub().start()
            tools.set_hub(hub)
            try:
                tracing.start_request()
                chart = await tools.compute_chart(parse(PROFILE, ref).birth, ref)
                return chart["months"]
            finally:
                await hub.close()
        return asyncio.run(run())

    def test_pinned_window(self):
        months = self.window("2026-10-21")
        self.assertEqual(len(months), 12)
        self.assertEqual((months[0]["month_ganzhi"], months[0]["start_date"]), ("戊戌", "2026-10-08"))
        self.assertEqual((months[-1]["month_ganzhi"], months[-1]["end_date"]), ("己酉", "2027-10-07"))

    def test_window_moves_with_reference_date(self):
        months = self.window("2027-01-15")
        self.assertEqual((months[0]["month_ganzhi"], months[0]["start_date"], months[0]["year_ganzhi"]),
                         ("辛丑", "2027-01-05", "丙午"))
        self.assertEqual(months[1]["year_ganzhi"], "丁未")   # 입춘에서 세운이 바뀐다


if __name__ == "__main__":
    unittest.main()
