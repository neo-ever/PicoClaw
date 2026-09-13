import json
from pathlib import Path

from picoclaw import (
    Agent,
    ChunkingConfig,
    ContextBudget,
    ContextChunk,
    ContextManager,
    LexicalSelector,
    Message,
    ModelResponse,
    RepositoryChunker,
    RepositoryEvidenceProvider,
    RunStore,
    TokenCounter,
    Workspace,
)
from picoclaw.tools import build_read_only_registry


class RecordingProvider:
    def __init__(self) -> None:
        self.requests = []

    def complete(self, messages, tools):
        del tools
        self.requests.append(tuple(messages))
        return ModelResponse(content="Evidence received.")


def make_chunk(
    chunk_id: str,
    path: str,
    content: str,
    *,
    source_sha256: str,
    tokens: int = 5,
) -> ContextChunk:
    return ContextChunk(
        id=chunk_id,
        path=path,
        start_line=1,
        end_line=2,
        content=content,
        source_sha256=source_sha256,
        token_count=tokens,
    )


def test_chunker_respects_workspace_filters_and_file_hashes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "auth.py"
    source.write_text(
        "def validate_refresh_token(token):\n    return token.is_valid\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private", encoding="utf-8")
    counter = TokenCounter("unknown-local-model")
    chunker = RepositoryChunker(
        Workspace(tmp_path),
        counter,
        ChunkingConfig(max_chunk_tokens=20, overlap_lines=0),
    )

    first = chunker.chunk_repository()
    second = chunker.chunk_repository()

    assert first
    assert {chunk.path for chunk in first} == {"src/auth.py"}
    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    old_hash = first[0].source_sha256

    source.write_text("def validate_refresh_token(token):\n    return False\n", encoding="utf-8")
    changed = chunker.chunk_repository()

    assert changed[0].source_sha256 != old_hash
    assert changed[0].id != first[0].id


def test_lexical_selector_prefers_relevant_chunks_and_suppresses_duplicates() -> None:
    relevant_content = "def validate_refresh_token(token): return token.is_valid"
    candidates = [
        make_chunk("auth-a", "src/auth.py", relevant_content, source_sha256="same"),
        make_chunk("auth-b", "copy/auth.py", relevant_content, source_sha256="same"),
        make_chunk(
            "payment",
            "src/payment.py",
            "def calculate_invoice_total(items): return sum(items)",
            source_sha256="payment",
        ),
    ]

    result = LexicalSelector().select(
        "Where is validate_refresh_token checked?",
        candidates,
        token_budget=10,
    )

    assert len(result.selected) == 1
    assert result.selected[0].chunk.id in {"auth-a", "auth-b"}
    assert result.selected_tokens == 5
    assert result.selected_tokens <= result.token_budget


def test_context_manager_injects_evidence_as_untrusted_system_data() -> None:
    chunk = make_chunk(
        "auth",
        "src/auth.py",
        "def validate_refresh_token(token): return token.is_valid",
        source_sha256="abc123",
    )
    manager = ContextManager(
        TokenCounter("unknown-local-model"),
        ContextBudget(
            max_input_tokens=500,
            max_memory_tokens=80,
            max_summary_tokens=60,
            max_evidence_tokens=180,
        ),
    )

    snapshot = manager.build(
        messages=[Message(role="user", content="Find token validation")],
        evidence_chunks=[chunk],
    )

    assert snapshot.evidence_items == 1
    assert snapshot.evidence_tokens > 0
    assert "untrusted file content" in snapshot.messages[0].content
    assert "src/auth.py" in snapshot.messages[0].content
    assert snapshot.rendered_tokens <= 500


def test_agent_selects_evidence_emits_trace_and_writes_report(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text(
        "def validate_refresh_token(token):\n    return token.is_valid\n",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    counter = TokenCounter("unknown-local-model")
    provider = RecordingProvider()
    evidence = RepositoryEvidenceProvider(workspace, counter, token_budget=160)

    result = Agent(
        provider,
        build_read_only_registry(workspace),
        context_manager=ContextManager(
            counter,
            ContextBudget(
                max_input_tokens=600,
                max_memory_tokens=80,
                max_summary_tokens=60,
                max_evidence_tokens=160,
            ),
        ),
        evidence_provider=evidence,
        run_store=RunStore(tmp_path / ".picoclaw" / "runs"),
    ).run("Explain validate_refresh_token")

    kinds = [event.kind for event in result.trace]
    assert "context_candidates_retrieved" in kinds
    assert "context_chunk_selected" in kinds
    assert "context_selection_finished" in kinds
    assert any("validate_refresh_token" in message.content for message in provider.requests[0])

    report = json.loads((Path(result.run_directory) / "report.json").read_text(encoding="utf-8"))
    assert report["evidence"]["enabled"] is True
    assert report["evidence"]["selected"] >= 1
    assert report["evidence"]["chunks"][0]["path"] == "auth.py"


def test_selector_failure_is_advisory_and_does_not_stop_agent(tmp_path: Path) -> None:
    class BrokenEvidenceProvider:
        def select(self, query):
            del query
            raise RuntimeError("selector unavailable")

    provider = RecordingProvider()
    result = Agent(
        provider,
        build_read_only_registry(Workspace(tmp_path)),
        context_manager=ContextManager(TokenCounter("unknown-local-model")),
        evidence_provider=BrokenEvidenceProvider(),
    ).run("Continue without preselected evidence")

    assert result.answer == "Evidence received."
    assert any(event.kind == "context_selection_failed" for event in result.trace)
