"""Run the read-only Agent Loop against a configured OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent
from .models import ModelProvider
from .providers import (
    OllamaConfig,
    OllamaTextProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
)
from .tools import build_read_only_registry
from .workspace import Workspace

DEFAULT_PROMPT = (
    "请调用 read_file 读取 README.md，然后告诉我项目名称。"
    "不要猜测，必须根据工具返回结果回答。"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PicoClaw real-provider read-only demo")
    parser.add_argument("prompt", nargs="*", help="Task for the coding agent")
    parser.add_argument("--cwd", default=".", help="Workspace root")
    parser.add_argument("--max-steps", type=int, default=4)
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

    try:
        workspace = Workspace(Path(args.cwd))
        provider = build_provider(args.provider)
        agent = Agent(provider, build_read_only_registry(workspace), max_steps=args.max_steps)
        result = agent.run(prompt)
    except (ValueError, ProviderError) as exc:
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


if __name__ == "__main__":
    main()
