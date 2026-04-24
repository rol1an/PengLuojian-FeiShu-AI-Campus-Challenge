import asyncio
import logging

import anthropic

from app.config import settings
from app.exceptions import LLMError

logger = logging.getLogger(__name__)
_client: anthropic.AsyncAnthropic | None = None


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise LLMError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _get_openai_client():
    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as e:
        raise LLMError("openai package is required for LLM_PROVIDER=openai|doubao") from e

    api_key = settings.OPENAI_API_KEY
    base_url = settings.OPENAI_BASE_URL or None

    if settings.LLM_PROVIDER.lower() == "doubao":
        api_key = settings.DOUBAO_API_KEY or settings.OPENAI_API_KEY
        base_url = settings.DOUBAO_BASE_URL or settings.OPENAI_BASE_URL or None

    if not api_key:
        raise LLMError(f"API key is missing for LLM_PROVIDER={settings.LLM_PROVIDER}")

    return AsyncOpenAI(api_key=api_key, base_url=base_url)


async def _call_anthropic(
    system: str,
    user: str,
    *,
    max_retries: int,
    temperature: float,
) -> str:
    client = get_anthropic_client()
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=settings.LLM_MAX_TOKENS,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            wait = 2**attempt
            logger.warning("LLM rate limit, retry %d in %ds: %s", attempt + 1, wait, e)
            await asyncio.sleep(wait)
            last_exc = e
        except anthropic.APIError as e:
            logger.error("LLM API error: %s", e)
            last_exc = e
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)

    raise LLMError(f"LLM failed after {max_retries} attempts") from last_exc


async def _call_openai_compatible(
    system: str,
    user: str,
    *,
    max_retries: int,
    temperature: float,
) -> str:
    client = _get_openai_client()

    try:
        from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError
    except ModuleNotFoundError as e:
        raise LLMError("openai package is required for LLM_PROVIDER=openai|doubao") from e

    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                temperature=temperature,
                max_tokens=settings.LLM_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            content = response.choices[0].message.content if response.choices else None
            if not content:
                raise LLMError("LLM returned empty content")
            return content
        except RateLimitError as e:
            wait = 2**attempt
            logger.warning("LLM rate limit, retry %d in %ds: %s", attempt + 1, wait, e)
            await asyncio.sleep(wait)
            last_exc = e
        except (APIError, APIConnectionError, APITimeoutError) as e:
            logger.error("LLM API error: %s", e)
            last_exc = e
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)

    raise LLMError(f"LLM failed after {max_retries} attempts") from last_exc


async def call_llm(
    system: str,
    user: str,
    *,
    max_retries: int = 3,
    temperature: float | None = None,
) -> str:
    """Call configured provider and return text content with retries."""
    provider = settings.LLM_PROVIDER.lower()
    effective_temp = temperature if temperature is not None else settings.LLM_TEMPERATURE

    if provider == "anthropic":
        return await _call_anthropic(
            system,
            user,
            max_retries=max_retries,
            temperature=effective_temp,
        )
    if provider in {"openai", "doubao"}:
        return await _call_openai_compatible(
            system,
            user,
            max_retries=max_retries,
            temperature=effective_temp,
        )
    raise LLMError(f"Unsupported LLM_PROVIDER: {settings.LLM_PROVIDER}")
