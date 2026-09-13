"""Preflight checks for handing Direct Delta training to a GPU environment."""

from __future__ import annotations

import argparse
import importlib.util
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .selection import read_delta_jsonl


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: str
    detail: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class DatasetAudit:
    train_examples: int
    validation_examples: int
    train_tasks: int
    validation_tasks: int
    overlapping_tasks: tuple[str, ...]
    train_positive: int
    validation_positive: int


def audit_datasets(train_path: Path, validation_path: Path) -> DatasetAudit:
    train = read_delta_jsonl(train_path)
    validation = read_delta_jsonl(validation_path)
    if not train or not validation:
        raise ValueError("training and validation datasets must not be empty")
    train_tasks = {example.task_id for example in train}
    validation_tasks = {example.task_id for example in validation}
    return DatasetAudit(
        train_examples=len(train),
        validation_examples=len(validation),
        train_tasks=len(train_tasks),
        validation_tasks=len(validation_tasks),
        overlapping_tasks=tuple(sorted(train_tasks & validation_tasks)),
        train_positive=sum(example.label for example in train),
        validation_positive=sum(example.label for example in validation),
    )


def run_preflight(
    model: str,
    train_path: Path,
    validation_path: Path,
    *,
    allow_cpu: bool = False,
) -> tuple[tuple[PreflightCheck, ...], DatasetAudit | None]:
    checks: list[PreflightCheck] = []
    required_packages = ("torch", "transformers", "accelerate", "numpy")
    available = {
        package: importlib.util.find_spec(package) is not None for package in required_packages
    }
    for package, found in available.items():
        checks.append(
            PreflightCheck(
                name=f"package:{package}",
                status="PASS" if found else "FAIL",
                detail="installed" if found else "missing; run uv sync --extra qwen",
            )
        )

    if available["transformers"]:
        import transformers

        classifier_available = hasattr(transformers, "Qwen3_5ForSequenceClassification")
        checks.append(
            PreflightCheck(
                name="qwen3.5-classifier",
                status="PASS" if classifier_available else "FAIL",
                detail=(
                    "Qwen3.5 sequence-classification support is available"
                    if classifier_available
                    else "installed Transformers is too old; install the current main branch"
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="qwen3.5-classifier",
                status="FAIL",
                detail="cannot inspect Qwen3.5 support before Transformers is installed",
            )
        )

    if available["torch"]:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        detail = "CUDA available"
        if cuda_available:
            detail = f"{torch.cuda.get_device_name(0)}; CUDA {torch.version.cuda}"
        elif allow_cpu:
            detail = "CUDA unavailable; CPU mode explicitly allowed and will be very slow"
        else:
            detail = "CUDA unavailable; use an NVIDIA GPU environment or pass --allow-cpu"
        checks.append(
            PreflightCheck(
                name="compute",
                status="PASS" if cuda_available or allow_cpu else "FAIL",
                detail=detail,
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="compute",
                status="FAIL",
                detail="cannot inspect CUDA before torch is installed",
            )
        )

    model_path = Path(model).expanduser()
    if model_path.exists():
        model_status = PreflightCheck("model", "PASS", str(model_path.resolve()))
    else:
        model_status = PreflightCheck(
            "model",
            "WARN",
            f"'{model}' is not local; training will try to download it",
            blocking=False,
        )
    checks.append(model_status)

    audit: DatasetAudit | None = None
    try:
        audit = audit_datasets(train_path, validation_path)
        if audit.overlapping_tasks:
            checks.append(
                PreflightCheck(
                    "dataset",
                    "FAIL",
                    f"task leakage detected: {', '.join(audit.overlapping_tasks[:5])}",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "dataset",
                    "PASS",
                    f"{audit.train_examples} train / {audit.validation_examples} validation; "
                    "task overlap 0",
                )
            )
        if min(audit.train_positive, audit.train_examples - audit.train_positive) == 0:
            checks.append(
                PreflightCheck(
                    "label-balance",
                    "FAIL",
                    "training data contains only one label",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "label-balance",
                    "PASS",
                    f"train positive {audit.train_positive}/{audit.train_examples}; "
                    f"validation positive {audit.validation_positive}/"
                    f"{audit.validation_examples}",
                )
            )
        if audit.train_examples < 100:
            checks.append(
                PreflightCheck(
                    "dataset-size",
                    "WARN",
                    "fewer than 100 training pairs; suitable for smoke tests, not conclusions",
                    blocking=False,
                )
            )
    except (OSError, ValueError) as exc:
        checks.append(PreflightCheck("dataset", "FAIL", str(exc)))

    return tuple(checks), audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Qwen Direct Delta training readiness")
    parser.add_argument("--model", required=True, help="Local model directory or remote model id")
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--allow-cpu", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    checks, audit = run_preflight(
        args.model,
        args.train,
        args.validation,
        allow_cpu=args.allow_cpu,
    )
    ready = not any(check.blocking and check.status == "FAIL" for check in checks)
    if args.as_json:
        print(
            json.dumps(
                {
                    "ready": ready,
                    "checks": [asdict(check) for check in checks],
                    "dataset": asdict(audit) if audit is not None else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("status | check | detail")
        for check in checks:
            print(f"{check.status} | {check.name} | {check.detail}")
        print(f"ready={str(ready).lower()}")
    if not ready:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
