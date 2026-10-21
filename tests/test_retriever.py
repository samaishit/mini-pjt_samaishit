"""말뭉치와 검색 테스트 (Bedrock 불필요: BM25 경로와 문서 적재만 검사).

실행: python -m unittest tests.test_retriever
"""
import ast
import json
import re
import unittest

from src import config
from src.retriever import _bm25, expand_query, korean_tokenizer, load_all_docs, load_place_docs, load_rule_docs

RULE_ID = re.compile(r"^rule:[a-z]+:[a-z0-9-]+$")
PLACE_ID = re.compile(r"^[RT]-[A-Z0-9-]+$")


class TestCorpus(unittest.TestCase):
    def test_places_db_size(self):
        regions = json.loads((config.PLACES_DIR / "regions.json").read_text(encoding="utf-8"))["regions"]
        travel = json.loads((config.PLACES_DIR / "travel.json").read_text(encoding="utf-8"))["travel"]
        self.assertEqual((len(regions), len(travel)), (30, 40))

    def test_rule_and_place_docs_loaded_together(self):
        rules, places = load_rule_docs(), load_place_docs()
        self.assertTrue(30 <= len(rules) <= 50, len(rules))
        self.assertEqual(len(places), 70)
        self.assertEqual(len(load_all_docs()), len(rules) + len(places))

    def test_doc_id_formats_unique(self):
        ids = [d.metadata["doc_id"] for d in load_all_docs()]
        self.assertEqual(len(ids), len(set(ids)))
        for d in load_all_docs():
            pattern = RULE_ID if d.metadata["doc_type"] == "rule" else PLACE_ID
            self.assertRegex(d.metadata["doc_id"], pattern)


class TestSearch(unittest.TestCase):
    def test_keyword_search_respects_doc_type_and_k(self):
        for doc_type, query in (("rule", "용신의 의미와 활용"), ("place", "바다가 가까운 조용한 주거지")):
            docs = _bm25(doc_type, 3).invoke(query)
            self.assertLessEqual(len(docs), 3)
            self.assertTrue(docs)
            self.assertTrue(all(d.metadata["doc_type"] == doc_type for d in docs))

    def test_domain_terms_not_split(self):
        self.assertIn("용신", korean_tokenizer("내 용신이 뭐야"))

    def test_query_expansion_is_deterministic(self):
        a = expand_query("여행지", ["火", "水"], ["己亥"])
        self.assertEqual(a, expand_query("여행지", ["火", "水"], ["己亥"]))
        self.assertIn("남쪽", a)
        self.assertIn("己亥", a)


class TestNoLLMImport(unittest.TestCase):
    def test_retriever_imports_no_llm_module(self):
        tree = ast.parse((config.PROJECT_ROOT / "src" / "retriever.py").read_text(encoding="utf-8"))
        imported = [n.module if isinstance(n, ast.ImportFrom) else a.name
                    for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in (n.names if isinstance(n, ast.Import) else [n])]
        names = " ".join(str(m) for m in imported) + " " + " ".join(
            a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names)
        for banned in ("ChatBedrock", "src.agent", "chat_models", "judge"):
            self.assertNotIn(banned, names)


if __name__ == "__main__":
    unittest.main()
