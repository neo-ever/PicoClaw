"""Optional batched HTTP service for a Qwen direct-delta regression checkpoint."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .selection import format_delta_prompt


def align_padding_config(model: Any, tokenizer: Any) -> int:
    """Keep tokenizer and composite Qwen configs aligned for batched inference."""
    if tokenizer.pad_token_id is None:
        raise RuntimeError("tokenizer does not define a usable padding token")
    pad_token_id = int(tokenizer.pad_token_id)
    model.config.pad_token_id = pad_token_id
    text_config = getattr(model.config, "text_config", None)
    if text_config is not None:
        text_config.pad_token_id = pad_token_id
    return pad_token_id


class QwenDeltaScorer:
    def __init__(self, model_path: str, max_length: int = 2048) -> None:
        try:
            import torch
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Qwen delta service requires torch and transformers in the current environment"
            ) from exc
        self.torch = torch
        self.max_length = max_length
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16 if self.device == "cuda" else torch.float32,
        ).to(self.device)
        align_padding_config(self.model, self.tokenizer)
        self.model.eval()

    def predict(self, items: list[dict[str, str]]) -> list[float]:
        prompts = [
            format_delta_prompt(
                item["question"],
                item["minus_context"],
                item["candidate_context"],
            )
            for item in items
        ]
        encoded = self.tokenizer(
            prompts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        encoded = {name: value.to(self.device) for name, value in encoded.items()}
        with self.torch.no_grad():
            logits = self.model(**encoded).logits.float().reshape(-1)
        return [float(value) for value in logits.cpu().tolist()]


def build_handler(scorer: QwenDeltaScorer, max_batch_size: int, max_body_bytes: int):
    class DeltaHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "contract": "direct_verifier_delta_regression",
                    "device": scorer.device,
                    "max_batch_size": max_batch_size,
                },
            )

        def do_POST(self) -> None:
            if self.path != "/predict_delta_batch":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > max_body_bytes:
                    raise ValueError("request body size is invalid")
                payload = json.loads(self.rfile.read(length))
                items = validate_items(payload, max_batch_size)
                deltas = scorer.predict(items)
                self._json(
                    HTTPStatus.OK,
                    {"deltas": [{"delta": delta} for delta in deltas]},
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except RuntimeError:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "model inference failed"})

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _json(self, status: HTTPStatus, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return DeltaHandler


def validate_items(payload: Any, max_batch_size: int) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise TypeError("payload.items must be an array")
    raw_items = payload["items"]
    if not 1 <= len(raw_items) <= max_batch_size:
        raise ValueError("batch size is outside the configured limit")
    items: list[dict[str, str]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise TypeError("each item must be an object")
        question = raw.get("question")
        minus = raw.get("minus_context", "")
        candidate = raw.get("candidate_context")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if not isinstance(minus, str) or not isinstance(candidate, str) or not candidate:
            raise ValueError("minus_context and candidate_context must be strings")
        items.append(
            {
                "question": question,
                "minus_context": minus,
                "candidate_context": candidate,
            }
        )
    return items


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a Qwen direct-delta checkpoint")
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6007)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--max-batch-size", type=int, default=32)
    parser.add_argument("--max-body-bytes", type=int, default=4_000_000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if min(args.port, args.max_length, args.max_batch_size, args.max_body_bytes) < 1:
        raise SystemExit("service limits must be positive")
    scorer = QwenDeltaScorer(args.model, args.max_length)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        build_handler(scorer, args.max_batch_size, args.max_body_bytes),
    )
    print(f"Qwen delta selector listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
