"""Async OpenRouter API client."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OpenRouterError(Exception):
    """Raised when OpenRouter API returns an error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterClient:
    """HTTP client for OpenRouter chat completions with tool support."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.openrouter_base_url.rstrip("/"),
            timeout=settings.openrouter_timeout,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/ai-concierge-vk",
                "X-Title": "AI-Concierge-VK",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = "auto",
    ) -> dict[str, Any]:
        """
        Send a chat completion request to OpenRouter.

        Returns the full API response dict including choices and tool_calls.
        """
        payload: dict[str, Any] = {
            "model": self._settings.openrouter_model,
            "messages": messages,
            "max_tokens": self._settings.openrouter_max_tokens,
            "temperature": 0.7,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            logger.error("OpenRouter HTTP error %s: %s", exc.response.status_code, detail)
            raise OpenRouterError(
                f"OpenRouter API error ({exc.response.status_code}): {detail}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            logger.error("OpenRouter request failed: %s", exc)
            raise OpenRouterError(
                "Не удалось подключиться к OpenRouter. Проверьте интернет и OPENROUTER_API_KEY."
            ) from exc

        data = response.json()
        if "error" in data:
            error_msg = data["error"].get("message", json.dumps(data["error"]))
            raise OpenRouterError(f"OpenRouter error: {error_msg}")

        return data

    @staticmethod
    def extract_assistant_message(response: dict[str, Any]) -> dict[str, Any]:
        """Extract the first assistant message from API response."""
        choices = response.get("choices", [])
        if not choices:
            raise OpenRouterError("OpenRouter returned empty choices")
        return choices[0]["message"]
