"""Optional Qwen regression fine-tuning entry for the aligned delta contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .selection import format_delta_prompt, read_delta_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fine-tune Qwen to predict verifier delta")
    parser.add_argument("--model", required=True, help="Base Qwen model path or Hugging Face id")
    parser.add_argument("--train", type=Path, required=True, help="Training delta JSONL")
    parser.add_argument("--validation", type=Path, required=True, help="Validation delta JSONL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=16)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if min(args.max_length, args.batch_size, args.gradient_accumulation) < 1:
        raise SystemExit("training lengths and batch values must be positive")
    try:
        import numpy as np
        import torch
        from torch.utils.data import Dataset
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            DataCollatorWithPadding,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise SystemExit(
            "Qwen training requires torch, transformers, accelerate, and numpy. "
            "Install them in a GPU-compatible environment."
        ) from exc

    train_examples = read_delta_jsonl(args.train)
    validation_examples = read_delta_jsonl(args.validation)
    if not train_examples or not validation_examples:
        raise SystemExit("training and validation datasets must not be empty")
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    class DeltaTorchDataset(Dataset):
        def __init__(self, examples) -> None:
            self.examples = examples

        def __len__(self) -> int:
            return len(self.examples)

        def __getitem__(self, index: int) -> dict:
            example = self.examples[index]
            encoded = tokenizer(
                format_delta_prompt(
                    example.query,
                    example.minus_context,
                    example.candidate_context,
                ),
                truncation=True,
                max_length=args.max_length,
            )
            encoded["labels"] = float(example.delta)
            return encoded

    model = AutoModelForSequenceClassification.from_pretrained(
        args.model,
        num_labels=1,
        problem_type="regression",
        trust_remote_code=True,
        torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
    )
    model.config.pad_token_id = tokenizer.pad_token_id
    if hasattr(model, "gradient_checkpointing_enable"):
        model.gradient_checkpointing_enable()

    def compute_metrics(evaluation) -> dict[str, float]:
        predictions = np.asarray(evaluation.predictions).reshape(-1)
        targets = np.asarray(evaluation.label_ids).reshape(-1)
        errors = predictions - targets
        sign_accuracy = float(np.mean((predictions > 0) == (targets > 0)))
        mae = float(np.mean(np.abs(errors)))
        rmse = float(np.sqrt(np.mean(errors**2)))
        pearson = (
            float(np.corrcoef(predictions, targets)[0, 1])
            if len(targets) > 1 and np.std(predictions) > 0 and np.std(targets) > 0
            else 0.0
        )
        return {
            "sign_accuracy": sign_accuracy,
            "mae": mae,
            "rmse": rmse,
            "pearson": pearson,
        }

    args.output.mkdir(parents=True, exist_ok=True)
    training_args = TrainingArguments(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        eval_strategy="epoch",
        save_strategy="epoch",
        logging_steps=10,
        load_best_model_at_end=True,
        metric_for_best_model="sign_accuracy",
        greater_is_better=True,
        bf16=torch.cuda.is_available() and torch.cuda.is_bf16_supported(),
        gradient_checkpointing=True,
        seed=args.seed,
        report_to=[],
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=DeltaTorchDataset(train_examples),
        eval_dataset=DeltaTorchDataset(validation_examples),
        data_collator=DataCollatorWithPadding(tokenizer),
        processing_class=tokenizer,
        compute_metrics=compute_metrics,
    )
    train_result = trainer.train()
    metrics = trainer.evaluate()
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))
    report = {
        "objective": "direct_verifier_delta_regression",
        "train_examples": len(train_examples),
        "validation_examples": len(validation_examples),
        "train_metrics": train_result.metrics,
        "validation_metrics": metrics,
    }
    (args.output / "delta-training-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
