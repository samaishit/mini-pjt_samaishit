# llm.py - Bedrock 채팅 모델 생성 + 한도 초과 시 대체 모델로 다시 호출
"""답변 LLM 이 스로틀링(분당·일일 토큰 한도 등)으로 실패하면 config.MODEL_FALLBACKS 순서대로 모델을 바꿔 같은 요청을 다시 보낸다.

- 대상 오류: Bedrock ClientError 중 THROTTLE_CODES (한도·일시 불가). 입력 오류(ValidationException)나 권한 오류는 그대로 올린다.
- ChatBedrockConverse 의 실제 호출 지점(_generate, _stream)만 감싸므로 도구 호출(bind_tools)·구조화 출력·Supervisor 에 모두 적용된다.
- 대체가 일어나면 trace 에 llm:fallback 단계(원래 모델 → 대체 모델, 오류 코드)를 남긴다.
- 심사 모델(JUDGE_MODEL)에는 쓰지 않는다 (라운드 간 비교를 위해 심사 모델은 고정).
"""
import logging
import re
from typing import Any, Iterator

from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from langchain_aws import ChatBedrockConverse
from pydantic import Field

from src import config

log = logging.getLogger("saju.llm")

# 다른 모델로 바꾸면 풀릴 수 있는 오류 (한도 초과·일시적으로 쓸 수 없음)
THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException",
                  "ServiceUnavailableException", "ModelNotReadyException"}


def throttle_code(e: BaseException) -> str | None:
    """대체 모델로 넘어갈 오류면 그 코드를, 아니면 None 을 돌려준다."""
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code")
        return code if code in THROTTLE_CODES else None
    return None


def short_model(model_id: str) -> str:
    """trace 용 모델 이름 (버전 날짜 제거: 8자리 날짜는 trace 마스킹이 생년월일로 보고 가린다)."""
    return re.sub(r"-\d{8}-v\d+:\d+$", "", model_id)


def _note_fallback(src: str, dst: str, code: str) -> None:
    log.warning("model fallback: %s -> %s (%s)", src, dst, code)
    try:
        from src.tracing import current
        current().add_step("llm:fallback", short_model(src), f"{code} → {short_model(dst)}", type="llm",
                           name="model_fallback")
    except Exception:   # 요청 밖(테스트·스크립트)에서는 trace 가 없다
        pass


class FallbackChatBedrockConverse(ChatBedrockConverse):
    """한도 초과 시 fallback_models 순서대로 모델을 바꿔 다시 호출하는 ChatBedrockConverse."""

    fallback_models: list[str] = Field(default_factory=list)

    def _with_model(self, model_id: str) -> "FallbackChatBedrockConverse":
        return self.model_copy(update={"model_id": model_id, "fallback_models": []})

    def _candidates(self) -> list[str]:
        return [m for m in self.fallback_models if m != self.model_id]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        try:
            return super()._generate(messages, stop, run_manager, **kwargs)
        except ClientError as e:
            code = throttle_code(e)
            if not code or not self._candidates():
                raise
            last, src = e, self.model_id
        for model_id in self._candidates():
            _note_fallback(src, model_id, code)
            try:
                return self._with_model(model_id)._generate(messages, stop, run_manager, **kwargs)
            except ClientError as e:
                code = throttle_code(e)
                if not code:
                    raise
                last, src = e, model_id
        raise last

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any) -> Iterator:
        # 스로틀링은 첫 응답 조각 전에 나므로, 조각을 내보내기 전 오류에서만 다음 모델로 넘어간다
        models = [self.model_id] + self._candidates()
        for i, model_id in enumerate(models):
            target = self if i == 0 else self._with_model(model_id)
            started = False
            try:
                for chunk in ChatBedrockConverse._stream(target, messages, stop, run_manager, **kwargs):
                    started = True
                    yield chunk
                return
            except ClientError as e:
                code = throttle_code(e)
                if started or not code or i == len(models) - 1:
                    raise
                _note_fallback(model_id, models[i + 1], code)


def make_llm(max_tokens: int = 4096, model: str | None = None, temperature: float = 0) -> FallbackChatBedrockConverse:
    """답변·에이전트·입력 가드용 LLM. 한도 초과 시 config.MODEL_FALLBACKS 로 넘어간다."""
    # 한도 초과는 같은 모델로 오래 재시도하기보다 빨리 대체 모델로 넘기는 편이 낫다 (재시도 3회, adaptive)
    boto_config = BotoConfig(read_timeout=config.BEDROCK_READ_TIMEOUT_S, connect_timeout=10,
                             retries={"max_attempts": 3, "mode": "adaptive"})
    return FallbackChatBedrockConverse(model=model or config.MODEL, region_name=config.REGION, temperature=temperature,
                                       max_tokens=max_tokens, config=boto_config,
                                       fallback_models=list(config.MODEL_FALLBACKS))
