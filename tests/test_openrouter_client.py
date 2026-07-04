"""OpenRouter client tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.clients.openrouter import OpenRouterClient, OpenRouterError


@pytest.mark.asyncio
async def test_chat_completion_success():
    settings = MagicMock()
    settings.openrouter_base_url = "https://openrouter.ai/api/v1"
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.openrouter_timeout = 30.0
    settings.openrouter_max_tokens = 512
    settings.salon_name = "Test Salon"

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Привет!"}}]
    }

    client = OpenRouterClient(settings)
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        data = await client.chat_completion([{"role": "user", "content": "Hi"}])
        assert data["choices"][0]["message"]["content"] == "Привет!"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_http_error():
    settings = MagicMock()
    settings.openrouter_base_url = "https://openrouter.ai/api/v1"
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.openrouter_timeout = 30.0
    settings.openrouter_max_tokens = 512
    settings.salon_name = "Test Salon"

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(401, request=request, text="Unauthorized")

    client = OpenRouterClient(settings)
    with patch.object(
        client._client,
        "post",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("error", request=request, response=response),
    ):
        with pytest.raises(OpenRouterError) as exc_info:
            await client.chat_completion([{"role": "user", "content": "Hi"}])
        assert exc_info.value.status_code == 401
    await client.close()
