"""Text embeddings behind a provider-neutral interface.

The vector width is fixed by the database column, so every provider must return
`dimensions` floats. Changing model or width means a migration and a re-embed.
"""

import hashlib
import math
from typing import Protocol

from app.config import Settings


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic, offline vectors for tests and development without an API key.

    Each word hashes to a few fixed vector positions, so texts that share words land close
    together. That's enough to test ranking logic, but it is not semantic.
    """

    def __init__(self, dimensions: int) -> None:
        self.model = "fake-hash-v1"
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dimensions
            for word in text.lower().split():
                word = "".join(ch for ch in word if ch.isalnum())
                if not word:
                    continue
                digest = hashlib.sha256(word.encode()).digest()
                for i in range(0, 12, 3):
                    idx = int.from_bytes(digest[i : i + 2], "big") % self.dimensions
                    vec[idx] += 1.0 if digest[i + 2] % 2 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class OpenAIEmbedder:
    BATCH = 96

    def __init__(self, api_key: str, model: str, dimensions: int) -> None:
        from openai import OpenAI

        if not api_key:
            raise ValueError("KLEANDBM_OPENAI_API_KEY must be set for the openai embedding provider")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH):
            batch = texts[start : start + self.BATCH]
            response = self.client.embeddings.create(model=self.model, input=batch, dimensions=self.dimensions)
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors


def embedder_from_settings(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings.openai_api_key, settings.embedding_model, settings.embedding_dimensions)
    if settings.embedding_provider == "fake":
        return FakeEmbedder(settings.embedding_dimensions)
    raise ValueError(f"unknown embedding provider {settings.embedding_provider!r}")
