import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from picoclaw import (
    Agent,
    ContextChunk,
    HttpDeltaSelector,
    HttpSelectorConfig,
    RepositoryEvidenceProvider,
    RuntimeIdentity,
    ScoreCache,
    Workspace,
)
from picoclaw.tools import build_read_only_registry


def make_chunk(chunk_id: str, path: str, content: str, tokens: int = 5) -> ContextChunk:
    return ContextChunk(
        id=chunk_id,
        path=path,
        start_line=1,
        end_line=2,
        content=content,
        source_sha256=f"sha-{chunk_id}",
        token_count=tokens,
    )


class RecordingScoreTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping, float]] = []

    def post_json(self, url, payload, timeout_seconds):
        self.calls.append((url, payload, timeout_seconds))
        scores = []
        for item in payload["items"]:
            context = item["context"]
            score = 0.0
            if "validate_refresh_token" in context:
                score += 10.0
            if "refresh token troubleshooting" in context:
                score += 1.0
            scores.append({"score": score})
        return {"scores": scores}


def relevant_candidates() -> list[ContextChunk]:
    return [
        make_chunk(
            "auth",
            "src/auth.py",
            "def validate_refresh_token(token): return token.is_valid",
        ),
        make_chunk(
            "docs",
            "docs/auth.md",
            "refresh token troubleshooting and common validation errors",
        ),
        make_chunk(
            "payment",
            "src/payment.py",
            "def calculate_invoice_total(items): return sum(items)",
        ),
    ]


def test_http_selector_batches_scores_and_selects_best_delta() -> None:
    transport = RecordingScoreTransport()
    selector = HttpDeltaSelector(
        HttpSelectorConfig(batch_size=8, candidate_pool_size=8),
        transport=transport,
    )

    result = selector.select(
        "Where does validate_refresh_token check a refresh token?",
        relevant_candidates(),
        token_budget=5,
    )

    assert [item.chunk.id for item in result.selected] == ["auth"]
    assert result.selected[0].score == 10.0
    assert result.http_requests == 1
    assert result.cache_misses == 3
    assert len(transport.calls) == 1
    assert transport.calls[0][0].endswith("/score_batch")
    assert len(transport.calls[0][1]["items"]) == 3
    assert all(item["question"] for item in transport.calls[0][1]["items"])


def test_http_selector_reuses_cached_scores_without_another_request() -> None:
    transport = RecordingScoreTransport()
    selector = HttpDeltaSelector(transport=transport)
    candidates = relevant_candidates()
    query = "Where does validate_refresh_token check a refresh token?"

    first = selector.select(query, candidates, token_budget=5)
    second = selector.select(query, candidates, token_budget=5)

    assert first.http_requests == 1
    assert second.http_requests == 0
    assert second.cache_hits == 3
    assert len(transport.calls) == 1


def test_http_selector_timeout_falls_back_to_lexical_selection() -> None:
    class TimeoutTransport:
        def post_json(self, url, payload, timeout_seconds):
            del url, payload, timeout_seconds
            raise TimeoutError("simulated timeout")

    selector = HttpDeltaSelector(transport=TimeoutTransport())

    result = selector.select(
        "Explain validate_refresh_token",
        relevant_candidates(),
        token_budget=5,
    )

    assert result.fallback_used is True
    assert result.fallback_reason == "TimeoutError"
    assert result.http_requests == 1
    assert result.selected
    assert result.selected[0].chunk.id == "auth"


def test_http_selector_invalid_response_falls_back() -> None:
    class InvalidTransport:
        def post_json(self, url, payload, timeout_seconds):
            del url, payload, timeout_seconds
            return {"scores": []}

    result = HttpDeltaSelector(transport=InvalidTransport()).select(
        "Explain validate_refresh_token",
        relevant_candidates(),
        token_budget=5,
    )

    assert result.fallback_used is True
    assert result.fallback_reason == "SelectorResponseError"


def test_http_selector_splits_large_batches() -> None:
    transport = RecordingScoreTransport()
    selector = HttpDeltaSelector(
        HttpSelectorConfig(batch_size=2, candidate_pool_size=5),
        transport=transport,
    )
    candidates = [
        make_chunk(
            f"auth-{index}",
            f"src/auth_{index}.py",
            f"validate refresh token helper_{index}",
        )
        for index in range(5)
    ]

    result = selector.select("validate refresh token", candidates, token_budget=5)

    assert result.http_requests == 3
    assert [len(call[1]["items"]) for call in transport.calls] == [2, 2, 2]


def test_remote_selector_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_remote"):
        HttpSelectorConfig(base_url="https://selector.example.com")

    config = HttpSelectorConfig(
        base_url="https://selector.example.com",
        allow_remote=True,
    )

    assert config.score_batch_url == "https://selector.example.com/score_batch"


def test_score_cache_is_bounded_lru() -> None:
    cache = ScoreCache(max_entries=2)
    cache.put("first", 1.0)
    cache.put("second", 2.0)
    assert cache.get("first") == (True, 1.0)

    cache.put("third", 3.0)

    assert cache.get("second") == (False, None)
    assert cache.get("first") == (True, 1.0)
    assert len(cache) == 2


def test_default_transport_matches_real_score_batch_wire_contract() -> None:
    class ScoreHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            scores = [
                {"score": 5.0 if "validate_refresh_token" in item["context"] else 0.0}
                for item in payload["items"]
            ]
            body = json.dumps({"scores": scores}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), ScoreHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        selector = HttpDeltaSelector(
            HttpSelectorConfig(base_url=f"http://127.0.0.1:{server.server_port}")
        )
        result = selector.select(
            "Explain validate_refresh_token",
            relevant_candidates(),
            token_budget=5,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.fallback_used is False
    assert result.selected[0].chunk.id == "auth"
    assert result.http_requests == 1


def test_selector_configuration_is_part_of_checkpoint_runtime_identity(tmp_path) -> None:
    class Provider:
        config = SimpleNamespace(model="test-model")

        def complete(self, messages, tools):
            raise AssertionError("not called")

    class Counter:
        @staticmethod
        def count_text(text):
            return len(text.split())

        @staticmethod
        def truncate_text(text, limit):
            return " ".join(text.split()[:limit])

    workspace = Workspace(tmp_path)

    def identity(batch_size: int) -> RuntimeIdentity:
        evidence = RepositoryEvidenceProvider(
            workspace,
            Counter(),
            selector=HttpDeltaSelector(HttpSelectorConfig(batch_size=batch_size)),
        )
        agent = Agent(
            Provider(),
            build_read_only_registry(workspace),
            evidence_provider=evidence,
        )
        return RuntimeIdentity.from_agent(agent)

    first = identity(4)
    changed = identity(8)

    assert first.evidence_config_sha256 != changed.evidence_config_sha256
    assert first.digest != changed.digest
