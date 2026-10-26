"""LLM 어댑터. 보고서 생성기는 complete()만 사용하므로 제공자를 바꿔도 나머지 코드는 그대로다.

API 키는 코드·로그에 남기지 않는다. 키는 SDK가 환경변수(ANTHROPIC_API_KEY 등)에서 직접 읽는다.
"""
from typing import Callable, Protocol


class LLMError(Exception):
    """API 실패·거절·빈 응답 등. 메시지에는 키나 프롬프트 내용을 넣지 않는다."""


class LLMAdapter(Protocol):
    model_name: str

    def complete(self, system: str, user: str, schema: dict) -> str: ...


class MockAdapter:
    """테스트·오프라인 시연용. 고정 문자열을 돌려주거나, 함수를 호출하거나, 예외를 던진다."""

    def __init__(self, response: str | Callable[[str, str], str] | Exception, model_name: str = "mock"):
        self.response, self.model_name = response, model_name
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response(system, user) if callable(self.response) else self.response


# 구조화 출력에 보낼 스키마에서 제거할 키 (길이·개수 제약은 응답 수신 후 Pydantic이 검증)
_DROP_KEYS = {"title", "minLength", "maxLength", "minItems", "maxItems", "default"}


def api_schema(schema):
    if isinstance(schema, dict):
        return {k: api_schema(v) for k, v in schema.items() if k not in _DROP_KEYS}
    if isinstance(schema, list):
        return [api_schema(v) for v in schema]
    return schema


class AnthropicAdapter:
    """Claude API (공식 anthropic SDK). `pip install -e ".[llm]"` 필요."""

    def __init__(self, model_name: str = "claude-opus-5-5", effort: str = "high", timeout: float = 300.0):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError('anthropic 패키지가 없습니다: pip install -e ".[llm]"') from e
        self._anthropic = anthropic
        self.model_name, self.effort = model_name, effort
        self._client = anthropic.Anthropic(timeout=timeout, max_retries=2)

    def complete(self, system: str, user: str, schema: dict) -> str:
        a = self._anthropic
        try:
            # 안전 분류기가 거절하면 서버가 권장 모델로 재시도 (fallbacks="default")
            resp = self._client.beta.messages.create(
                model=self.model_name,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": api_schema(schema)}},
            )
        except a.APITimeoutError as e:
            raise LLMError("AI API 응답 시간 초과") from e
        except a.APIConnectionError as e:
            raise LLMError("AI API 연결 실패") from e
        except a.RateLimitError as e:
            raise LLMError("AI API 요청 한도 초과") from e
        except a.AuthenticationError as e:
            raise LLMError("AI API 인증 실패 (API 키 설정 확인)") from e
        except a.APIStatusError as e:
            raise LLMError(f"AI API 오류 (HTTP {e.status_code})") from e
        except TypeError as e:
            # 인증 정보가 전혀 없으면 SDK가 요청 직전에 TypeError를 던진다
            if "authentication" not in str(e).lower():
                raise
            raise LLMError("AI API 인증 정보가 없습니다 (ANTHROPIC_API_KEY 설정 필요)") from e
        if resp.stop_reason == "refusal":
            raise LLMError("AI 모델이 요청을 거절했습니다")
        if resp.stop_reason == "max_tokens":
            raise LLMError("AI 응답이 길이 제한으로 잘렸습니다")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text.strip():
            raise LLMError("AI 응답이 비어 있습니다")
        return text
