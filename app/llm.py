"""LLM 호출 래퍼. config.yaml의 ai.provider로 gemini / anthropic 선택."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional, Protocol

from .config import AISettings


class LLMError(Exception):
    MESSAGES = {
        "NO_KEY": "AI API 키가 없습니다. .env에 GEMINI_API_KEY(또는 ANTHROPIC_API_KEY)를 넣어 주세요.",
        "RATE_LIMIT": "AI 사용 한도에 걸렸습니다. 잠시 후 다시 시도해 주세요 (무료 키는 분당·일일 한도가 있습니다).",
        "AUTH": "AI API 키가 올바르지 않습니다.",
        "MODEL": "설정한 AI 모델을 찾을 수 없습니다. config.yaml의 모델 이름을 확인해 주세요.",
        "BAD_RESPONSE": "AI 응답을 해석하지 못했습니다.",
        "UNAVAILABLE": "AI 서버가 응답하지 않습니다.",
        "NOT_INSTALLED": "AI 라이브러리가 설치되지 않았습니다.",
        "VIDEO_UNSUPPORTED": "이 영상은 직접 분석할 수 없습니다.",
    }

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{self.MESSAGES.get(code, code)} {detail}".strip())


class LLM(Protocol):
    name: str
    supports_video: bool

    def generate_json(self, prompt: str, schema: dict, *, model: str, temperature: float,
                      system: str = "", video_url: Optional[str] = None) -> dict: ...


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise LLMError("BAD_RESPONSE", text[:120])


def _with_retry(fn, retries: int = 3):
    """429, 5xx만 2s / 6s / 15s 간격으로 재시도."""
    delays = [2, 6, 15]
    for i in range(retries + 1):
        try:
            return fn()
        except LLMError as e:
            if e.code in ("RATE_LIMIT", "UNAVAILABLE") and i < retries:
                time.sleep(delays[min(i, len(delays) - 1)])
                continue
            raise


class GeminiLLM:
    name = "gemini"
    supports_video = True

    def __init__(self, api_key: str):
        if not api_key:
            raise LLMError("NO_KEY")
        try:
            from google import genai
        except ImportError as e:
            raise LLMError("NOT_INSTALLED", "pip install google-genai") from e
        self._genai = genai
        self.client = genai.Client(api_key=api_key)

    def generate_json(self, prompt: str, schema: dict, *, model: str, temperature: float,
                      system: str = "", video_url: Optional[str] = None) -> dict:
        from google.genai import errors, types

        contents: list[Any] = []
        if video_url:
            contents.append(types.Part(file_data=types.FileData(file_uri=video_url)))
        contents.append(prompt)
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            response_mime_type="application/json",
            response_json_schema=schema,
        )

        def call():
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
            except errors.APIError as e:
                code = getattr(e, "code", 0) or 0
                msg = str(getattr(e, "message", "") or e)[:160]
                if code == 429:
                    raise LLMError("RATE_LIMIT", msg) from e
                if code in (401, 403):
                    raise LLMError("AUTH", msg) from e
                if code == 404:
                    raise LLMError("MODEL", f"{model}: {msg}") from e
                if code == 400 and video_url:
                    raise LLMError("VIDEO_UNSUPPORTED", msg) from e
                if code >= 500:
                    raise LLMError("UNAVAILABLE", msg) from e
                raise LLMError("BAD_RESPONSE", msg) from e
            return _extract_json(resp.text or "")

        return _with_retry(call)


class AnthropicLLM:
    name = "anthropic"
    supports_video = False

    def __init__(self, api_key: str):
        if not api_key:
            raise LLMError("NO_KEY")
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("NOT_INSTALLED", "pip install anthropic") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate_json(self, prompt: str, schema: dict, *, model: str, temperature: float,
                      system: str = "", video_url: Optional[str] = None) -> dict:
        a = self._anthropic
        sys_text = (system + "\n\n" if system else "") + \
            "반드시 다음 JSON 스키마에 맞는 JSON 객체 하나만 출력하세요. 다른 설명은 쓰지 마세요.\n" + \
            json.dumps(schema, ensure_ascii=False)

        def call():
            try:
                msg = self.client.messages.create(model=model, max_tokens=2000, temperature=temperature,
                                                  system=sys_text, messages=[{"role": "user", "content": prompt}])
            except a.RateLimitError as e:
                raise LLMError("RATE_LIMIT", str(e)[:160]) from e
            except a.AuthenticationError as e:
                raise LLMError("AUTH", str(e)[:160]) from e
            except a.NotFoundError as e:
                raise LLMError("MODEL", f"{model}") from e
            except a.APIStatusError as e:
                raise LLMError("UNAVAILABLE" if e.status_code >= 500 else "BAD_RESPONSE", str(e)[:160]) from e
            except a.APIConnectionError as e:
                raise LLMError("UNAVAILABLE", str(e)[:160]) from e
            text = "".join(getattr(b, "text", "") for b in msg.content)
            return _extract_json(text)

        return _with_retry(call)


def make_llm(ai: AISettings) -> LLM:
    if ai.provider == "gemini":
        return GeminiLLM(ai.api_key())
    if ai.provider == "anthropic":
        return AnthropicLLM(ai.api_key())
    raise LLMError("MODEL", f"알 수 없는 provider: {ai.provider}")
