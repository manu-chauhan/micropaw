"""Tests for embedding utilities — cosine similarity, serialization."""

from __future__ import annotations

import pytest

from micropaw.memory.embeddings import (
    NullEmbedding,
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_mismatched_lengths(self):
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestSerialization:
    def test_round_trip(self):
        v = [1.0, 2.5, -3.0]
        serialized = serialize_embedding(v)
        assert serialized is not None
        deserialized = deserialize_embedding(serialized)
        assert deserialized == v

    def test_empty_embedding(self):
        assert serialize_embedding([]) is None
        assert deserialize_embedding(None) == []
        assert deserialize_embedding("") == []


class TestNullEmbedding:
    async def test_returns_empty(self):
        ne = NullEmbedding()
        result = await ne.embed("test text")
        assert result == []
        assert ne.dimensions == 0
