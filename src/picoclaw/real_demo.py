"""Run the read-only Agent Loop against a configured OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent
from .checkpoint import CheckpointError, CheckpointStore
from .context_manager import ContextBudget, ContextManager, TokenCounter
from .goal_loop import GoalRunner
from .memory import MemoryManager
from .models import ModelProvider
from .providers import (
    OllamaConfig,
    OllamaTextProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
)
from .run_store import RunStore
from .selection import (
    ChunkingConfig,
    DirectDeltaHttpSelector,
    HttpDeltaSelector,
    HttpSelectorConfig,
    LexicalSelector,
    RepositoryEvidenceProvider,
)
from .skills import SkillCatalog
from .task_state import GoalBudget, TokenPricing
from .tools import build_coding_registry
from .verifier import FileContentVerifier
from .workspace import Workspace

DEFAULT_PROMPT = (
    "请调用 read_file 读取 README.md，然后告诉我项目名称。不要猜测，必须根据工具返回结果回答。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PicoClaw real-provider coding-agent demo")
    parser.add_argument("prompt", nargs="*", help="Task for the coding agent")
    parser.add_argument("--cwd", default=".", help="Workspace root")
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--max-input-tokens", type=int, default=4096)
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="Preselect relevant repository chunks before the first model turn",
    )
    parser.add_argument("--evidence-tokens", type=int, default=1200)
    parser.add_argument("--chunk-tokens", type=int, default=320)
    parser.add_argument(
        "--evidence-selector",
        choices=("lexical", "http", "http-direct"),
        default="lexical",
        help="Repository evidence ranking strategy",
    )
    parser.add_argument(
        "--selector-url",
        default=os.environ.get("PICOCLAW_SELECTOR_URL", "http://127.0.0.1:6006"),
        help="Base URL of the 0.8B selector service",
    )
    parser.add_argument("--selector-timeout", type=float, default=8.0)
    parser.add_argument("--selector-batch-size", type=int, default=8)
    parser.add_argument("--selector-cache-size", type=int, default=2048)
    parser.add_argument("--selector-candidate-pool", type=int, default=24)
    parser.add_argument("--selector-min-delta", type=float, default=0.0)
    parser.add_argument(
        "--allow-remote-selector",
        action="store_true",
        help="Allow sending selected repository candidates to a non-loopback selector URL",
    )
    parser.add_argument(
        "--skills-dir",
        default="skills",
        help="Workspace-relative skill root; only matched SKILL.md bodies are loaded",
    )
    parser.add_argument("--verify-file", help="Enable Goal Loop and verify this file")
    parser.add_argument(
        "--verify-contains",
        help="Required substring for --verify-file",
    )
    parser.add_argument("--resume", action="store_true", help="Resume a valid goal checkpoint")
    parser.add_argument("--goal-attempts", type=int, default=3)
    parser.add_argument("--goal-model-steps", type=int, default=18)
    parser.add_argument("--goal-tool-calls", type=int, default=24)
    parser.add_argument("--goal-total-tokens", type=int, default=50_000)
    parser.add_argument("--goal-max-cost-usd", type=float)
    parser.add_argument("--input-cost-per-million-usd", type=float)
    parser.add_argument("--output-cost-per-million-usd", type=float)
    parser.add_argument("--goal-seconds", type=float, default=300.0)
    parser.add_argument("--goal-stagnation", type=int, default=2)
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for write and execute tools",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "ollama"),
        default=os.environ.get("PICOCLAW_PROVIDER", "openai"),
    )
    return parser


def build_provider(provider_name: str) -> ModelProvider:
    if provider_name == "ollama":
        return OllamaTextProvider(OllamaConfig.from_env())
    return OpenAICompatibleProvider(ProviderConfig.from_env())


def main() -> None:
    args = build_parser().parse_args()
    prompt = " ".join(args.prompt).strip() or DEFAULT_PROMPT
    if bool(args.verify_file) != bool(args.verify_contains):
        raise SystemExit(
            "PicoClaw error: --verify-file and --verify-contains must be used together"
        )
    if args.resume and not args.verify_file:
        raise SystemExit("PicoClaw error: --resume requires Goal Loop verification options")
    pricing_values = (
        args.input_cost_per_million_usd,
        args.output_cost_per_million_usd,
    )
    if any(value is not None for value in pricing_values) and not all(
        value is not None for value in pricing_values
    ):
        raise SystemExit("PicoClaw error: both input and output token prices are required")
    if args.goal_max_cost_usd is not None and pricing_values[0] is None:
        raise SystemExit("PicoClaw error: a cost budget requires input/output token prices")

    try:
        workspace = Workspace(Path(args.cwd))
        provider = build_provider(args.provider)
        tools = build_coding_registry(workspace, approval_mode=args.approval)
        run_store = RunStore(workspace.root / ".picoclaw" / "runs")
        model_name = provider.config.model
        token_counter = TokenCounter(model_name)
        context_manager = ContextManager(
            token_counter,
            ContextBudget(
                max_input_tokens=args.max_input_tokens,
                max_evidence_tokens=args.evidence_tokens,
            ),
        )
        evidence_provider = None
        if args.evidence:
            lexical_selector = LexicalSelector()
            selector = lexical_selector
            if args.evidence_selector == "http":
                selector = HttpDeltaSelector(
                    HttpSelectorConfig(
                        base_url=args.selector_url,
                        timeout_seconds=args.selector_timeout,
                        batch_size=args.selector_batch_size,
                        cache_entries=args.selector_cache_size,
                        candidate_pool_size=args.selector_candidate_pool,
                        min_delta=args.selector_min_delta,
                        allow_remote=args.allow_remote_selector,
                    ),
                    fallback=lexical_selector,
                )
            elif args.evidence_selector == "http-direct":
                selector = DirectDeltaHttpSelector(
                    HttpSelectorConfig(
                        base_url=args.selector_url,
                        timeout_seconds=args.selector_timeout,
                        batch_size=args.selector_batch_size,
                        cache_entries=args.selector_cache_size,
                        candidate_pool_size=args.selector_candidate_pool,
                        min_delta=args.selector_min_delta,
                        allow_remote=args.allow_remote_selector,
                    ),
                    fallback=lexical_selector,
                )
            evidence_provider = RepositoryEvidenceProvider(
                workspace,
                token_counter,
                selector=selector,
                token_budget=args.evidence_tokens,
                chunking=ChunkingConfig(max_chunk_tokens=args.chunk_tokens),
            )
        memory_manager = MemoryManager(workspace)
        skill_catalog = SkillCatalog([workspace.resolve(args.skills_dir)])
        agent = Agent(
            provider,
            tools,
            max_steps=args.max_steps,
            run_store=run_store,
            context_manager=context_manager,
            memory_manager=memory_manager,
            skill_catalog=skill_catalog,
            evidence_provider=evidence_provider,
        )
        if args.verify_file:
            pricing = (
                TokenPricing(pricing_values[0], pricing_values[1])
                if pricing_values[0] is not None and pricing_values[1] is not None
                else None
            )
            goal_result = GoalRunner(
                agent,
                workspace,
                FileContentVerifier(
                    args.verify_file,
                    args.verify_contains,
                    exact=False,
                ),
                CheckpointStore(workspace.root / ".picoclaw" / "checkpoints"),
                budget=GoalBudget(
                    max_attempts=args.goal_attempts,
                    max_model_steps=args.goal_model_steps,
                    max_tool_calls=args.goal_tool_calls,
                    max_total_tokens=args.goal_total_tokens,
                    max_cost_usd=args.goal_max_cost_usd,
                    max_seconds=args.goal_seconds,
                    max_stagnant_attempts=args.goal_stagnation,
                ),
                pricing=pricing,
            ).run(prompt, resume=args.resume)
            print("=== Goal Result ===")
            print(f"status={goal_result.state.status.value}")
            print(f"reason={goal_result.state.stopped_reason}")
            print(f"attempts={goal_result.state.attempts}")
            print(f"model_steps={goal_result.state.model_steps}")
            print(f"tool_calls={goal_result.state.tool_calls}")
            print(f"prompt_tokens={goal_result.state.prompt_tokens}")
            print(f"completion_tokens={goal_result.state.completion_tokens}")
            print(f"total_tokens={goal_result.state.total_tokens}")
            print(f"estimated_cost_usd={goal_result.state.estimated_cost_usd:.6f}")
            print(f"checkpoint={goal_result.checkpoint_path}")
            print(f"report={goal_result.report_path}")
            if not goal_result.succeeded:
                raise SystemExit(2)
            return
        result = agent.run(prompt)
    except (CheckpointError, ValueError, ProviderError) as exc:
        raise SystemExit(f"PicoClaw error: {exc}") from exc

    print("=== Final Answer ===")
    print(result.answer)
    print("\n=== Usage ===")
    print(
        f"prompt={result.usage.prompt_tokens}, "
        f"completion={result.usage.completion_tokens}, "
        f"total={result.usage.total_tokens}"
    )
    print("\n=== Trace ===")
    for event in result.trace:
        print(f"- {event.kind}: {event.detail}")
    if result.run_directory:
        print(f"\nRun artifacts: {result.run_directory}")


if __name__ == "__main__":
    main()
