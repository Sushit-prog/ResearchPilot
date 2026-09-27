from __future__ import annotations

from typing import Any

import httpx

from app.errors import LLMProviderError


class OpenAICompatibleProvider:
    """Chat-completions client for any OpenAI-compatible endpoint.

    Covers OpenAI itself, Gemini's OpenAI-compatible layer, Ollama, vLLM, and
    anything else that serves POST {base_url}/chat/completions with Bearer auth.
    Network, HTTP, and response-shape failures all surface as LLMProviderError;
    messages never include the API key or response bodies.
    """

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client

    async def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        client = self._client if self._client is not None else httpx.AsyncClient(
            timeout=self._timeout_s
        )
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            if response.status_code >= 400:
                raise LLMProviderError(
                    f"LLM endpoint returned HTTP {response.status_code}"
                )
            return _extract_content(response)
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"LLM request failed: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()


def _extract_content(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMProviderError("LLM endpoint returned a non-JSON body") from exc
    if not isinstance(body, dict):
        raise LLMProviderError("LLM endpoint returned a non-object JSON body")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderError("LLM response is missing choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LLMProviderError("LLM response is missing message content")
    return content
