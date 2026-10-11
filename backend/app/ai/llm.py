"""Thin wrapper around the Gemini API (google-genai SDK).

If no API key is configured, `enabled` is False and every caller falls back to a
deterministic, template-based answer - the app never breaks because of the LLM.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import settings

log = logging.getLogger("drivesense.llm")


class LLMError(Exception):
    pass


class GeminiClient:
    def __init__(self) -> None:
        self.model = settings.gemini_model
        self.embed_model = settings.gemini_embed_model
        self.enabled = bool(settings.gemini_api_key.strip())
        self._client = None
        self._sem = asyncio.Semaphore(2)   # be gentle with free-tier rate limits
        if self.enabled:
            from google import genai
            self._client = genai.Client(api_key=settings.gemini_api_key.strip())

    async def generate(self, system: str, prompt: str, temperature: float = 0.3) -> str:
        if not self.enabled:
            raise LLMError("Gemini API key not configured")
        from google.genai import errors, types
        config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        try:
            async with self._sem:
                resp = await asyncio.wait_for(
                    self._client.aio.models.generate_content(model=self.model, contents=prompt, config=config),
                    timeout=45)
        except asyncio.TimeoutError as exc:
            raise LLMError("Gemini request timed out") from exc
        except errors.APIError as exc:
            if getattr(exc, "code", None) == 429:
                raise LLMError("Gemini free-tier rate limit reached - try again in a minute") from exc
            raise LLMError(f"Gemini API error: {exc}") from exc
        text = (resp.text or "").strip()
        if not text:
            raise LLMError("Gemini returned an empty answer")
        return text

    async def embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        """task_type: RETRIEVAL_DOCUMENT for knowledge chunks, RETRIEVAL_QUERY for questions."""
        if not self.enabled:
            raise LLMError("Gemini API key not configured")
        from google.genai import errors, types
        out: list[list[float]] = []
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=settings.embed_dim)
        for i in range(0, len(texts), 50):
            batch = texts[i:i + 50]
            try:
                async with self._sem:
                    resp = await asyncio.wait_for(
                        self._client.aio.models.embed_content(model=self.embed_model, contents=batch, config=config),
                        timeout=60)
            except (asyncio.TimeoutError, errors.APIError) as exc:
                raise LLMError(f"Embedding failed: {exc}") from exc
            out.extend(list(e.values) for e in resp.embeddings)
        return out


llm = GeminiClient()
