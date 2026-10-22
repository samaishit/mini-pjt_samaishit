"""웹 화면이 조립한 출생 문장을 서버 파서가 그대로 읽는지 확인한다 (Bedrock 불필요).

문장 조립은 web/src/composer.ts 가 하고, 대응표 web/src/composer.cases.json 을 vitest 와 이 테스트가 함께 쓴다.
실행: python -m unittest tests.test_web_composer
"""
import json
import unittest
from dataclasses import asdict

from src import config
from src.birth_parser import parse

CASES = json.loads((config.PROJECT_ROOT / "web" / "src" / "composer.cases.json").read_text(encoding="utf-8"))["cases"]


class TestComposedSentences(unittest.TestCase):
    def test_parser_reads_every_composed_sentence(self):
        for case in CASES:
            r = parse(f"{case['sentence']} 내년 여행지 추천해줘.")
            self.assertIsNone(r.clarification, case["sentence"])
            self.assertEqual(asdict(r.birth), case["birth"], case["sentence"])
            self.assertNotIn(str(case["birth"]["year"]), r.masked_text, case["sentence"])


if __name__ == "__main__":
    unittest.main()
