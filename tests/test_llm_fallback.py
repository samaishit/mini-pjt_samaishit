"""한도 초과 시 대체 모델로 다시 호출하는 로직 테스트 (Bedrock 불필요: 실제 호출을 가짜로 바꿈).

실행: python -m unittest tests.test_llm_fallback
"""
import unittest
from unittest import mock

from botocore.exceptions import ClientError
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src import config, tracing
from src.llm import FallbackChatBedrockConverse, make_llm

PRIMARY = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
FALLBACKS = ["global.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-6"]


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "Too many tokens per day"}}, "Converse")


def fake_generate(behaviour: dict):
    """모델 id 별로 오류를 내거나 그 모델 이름으로 답한다. 호출 순서를 calls 에 남긴다."""
    calls = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_id)
        outcome = behaviour.get(self.model_id)
        if isinstance(outcome, Exception):
            raise outcome
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.model_id))])
    return _generate, calls


class TestModelFallback(unittest.TestCase):
    def setUp(self):
        self.model = FallbackChatBedrockConverse(model=PRIMARY, region_name="us-east-1", fallback_models=FALLBACKS)
        self.state = tracing.start_request()

    def run_with(self, behaviour):
        gen, calls = fake_generate(behaviour)
        with mock.patch.object(ChatBedrockConverse, "_generate", gen):
            try:
                return self.model.invoke([HumanMessage("hi")]).content, calls
            except ClientError as e:
                return e.response["Error"]["Code"], calls

    def test_primary_ok_no_fallback(self):
        out, calls = self.run_with({})
        self.assertEqual((out, calls), (PRIMARY, [PRIMARY]))

    def test_throttled_goes_to_global_sonnet_4_5(self):
        out, calls = self.run_with({PRIMARY: error("ThrottlingException")})
        self.assertEqual(out, FALLBACKS[0])
        self.assertEqual(calls, [PRIMARY, FALLBACKS[0]])
        step = [t for t in self.state.trace if t["step"] == "llm:fallback"][0]
        self.assertEqual((step["input"], step["output"]),
                         ("us.anthropic.claude-sonnet-4-5", "ThrottlingException → global.anthropic.claude-sonnet-4-5"))

    def test_second_fallback_is_sonnet_4_6(self):
        out, calls = self.run_with({PRIMARY: error("ThrottlingException"), FALLBACKS[0]: error("ThrottlingException")})
        self.assertEqual(out, FALLBACKS[1])
        self.assertEqual(calls, [PRIMARY] + FALLBACKS)

    def test_all_throttled_raises_last_error(self):
        out, calls = self.run_with({m: error("ThrottlingException") for m in [PRIMARY] + FALLBACKS})
        self.assertEqual(out, "ThrottlingException")
        self.assertEqual(len(calls), 3)

    def test_non_throttle_error_is_not_retried(self):
        out, calls = self.run_with({PRIMARY: error("ValidationException")})
        self.assertEqual((out, calls), ("ValidationException", [PRIMARY]))

    def test_structured_output_and_tools_use_fallback_too(self):
        bound = self.model.bind_tools([{"name": "noop", "description": "x", "input_schema": {"type": "object"}}])
        gen, calls = fake_generate({PRIMARY: error("ThrottlingException")})
        with mock.patch.object(ChatBedrockConverse, "_generate", gen):
            self.assertEqual(bound.invoke([HumanMessage("hi")]).content, FALLBACKS[0])


class TestConfig(unittest.TestCase):
    def test_default_fallback_order(self):
        self.assertEqual(config.DEFAULT_MODEL_FALLBACKS.split(","), FALLBACKS)

    def test_make_llm_wires_fallbacks(self):
        m = make_llm(max_tokens=10)
        self.assertEqual(m.fallback_models, config.MODEL_FALLBACKS)
        self.assertEqual(m.model_id, config.MODEL)


if __name__ == "__main__":
    unittest.main()
