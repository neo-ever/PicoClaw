"""Repository evidence selection for PicoClaw."""

from .cache import ScoreCache
from .chunker import ChunkingConfig, RepositoryChunker
from .dataset import (
    SCHEMA_VERSION,
    DeltaDatasetBuilder,
    DeltaExample,
    OutcomeScorer,
    format_delta_prompt,
    read_delta_jsonl,
    render_chunks,
    split_delta_examples,
    write_delta_jsonl,
)
from .full import FullContextSelector
from .http import (
    DirectDeltaHttpSelector,
    HttpDeltaSelector,
    HttpSelectorConfig,
    JsonTransport,
    SelectorResponseError,
    SelectorServiceError,
    UrllibJsonTransport,
)
from .lexical import LexicalSelector
from .linear import (
    DeltaMetrics,
    LinearDeltaModel,
    LinearDeltaSelector,
    delta_features,
    evaluate_linear_delta,
    train_linear_delta,
)
from .models import ContextChunk, ScoredChunk, SelectionResult
from .protocol import ContextSelector, EvidenceProvider
from .service import RepositoryEvidenceProvider

__all__ = [
    "SCHEMA_VERSION",
    "ChunkingConfig",
    "ContextChunk",
    "ContextSelector",
    "DeltaDatasetBuilder",
    "DeltaExample",
    "DeltaMetrics",
    "DirectDeltaHttpSelector",
    "EvidenceProvider",
    "FullContextSelector",
    "HttpDeltaSelector",
    "HttpSelectorConfig",
    "JsonTransport",
    "LexicalSelector",
    "LinearDeltaModel",
    "LinearDeltaSelector",
    "OutcomeScorer",
    "RepositoryChunker",
    "RepositoryEvidenceProvider",
    "ScoreCache",
    "ScoredChunk",
    "SelectionResult",
    "SelectorResponseError",
    "SelectorServiceError",
    "UrllibJsonTransport",
    "delta_features",
    "evaluate_linear_delta",
    "format_delta_prompt",
    "read_delta_jsonl",
    "render_chunks",
    "split_delta_examples",
    "train_linear_delta",
    "write_delta_jsonl",
]
