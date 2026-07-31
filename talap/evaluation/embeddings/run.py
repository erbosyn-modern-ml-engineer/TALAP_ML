"""Manual CLI for the offline embedding-provider evaluation harness.

Usage (from the repository root):

    # Jina v4
    python -m talap.evaluation.embeddings.run \
        --dataset data/evaluation/talap_embedding_eval.json \
        --provider jina --model jina-embeddings-v4

    # Jina v3
    python -m talap.evaluation.embeddings.run \
        --dataset data/evaluation/talap_embedding_eval.json \
        --provider jina --model jina-embeddings-v3

    # OpenAI small
    python -m talap.evaluation.embeddings.run \
        --dataset data/evaluation/talap_embedding_eval.json \
        --provider openai --model text-embedding-3-small

    # OpenAI large
    python -m talap.evaluation.embeddings.run \
        --dataset data/evaluation/talap_embedding_eval.json \
        --provider openai --model text-embedding-3-large

No provider/model is hardcoded as the winner; pick one and compare the JSON
reports. API keys come from ``--api-key`` or the provider environment
variable (``OPENAI_API_KEY`` / ``JINA_API_KEY``). The CLI fails safely and
never prints key values.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from talap.evaluation.embeddings.adapters import available_providers
from talap.evaluation.embeddings.runner import (
    EvaluationConfig,
    ProviderSkippedError,
    run_evaluation,
)

_PROVIDER_ENV_API_KEY = {"openai": "OPENAI_API_KEY", "jina": "JINA_API_KEY"}
_PROVIDER_ENV_BASE_URL = {"openai": "OPENAI_BASE_URL", "jina": "JINA_BASE_URL"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline embedding-provider evaluation for TALAP."
    )
    parser.add_argument("--dataset", required=True, help="Path to the JSON benchmark dataset.")
    parser.add_argument(
        "--provider",
        required=True,
        choices=available_providers(),
        help="Embedding provider to evaluate.",
    )
    parser.add_argument("--model", required=True, help="Provider model name.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the provider base URL (defaults to the provider env var).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Provider API key (defaults to the provider environment variable).",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=None,
        help="Optional embedding dimensions to request.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Optional fixed task type for OpenAI-compatible endpoints.",
    )
    parser.add_argument(
        "--price-per-1m-tokens",
        type=float,
        default=None,
        help="USD per 1M tokens for cost estimation.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: <dataset-dir>/results/<provider>-<model>.json).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    api_key = args.api_key or os.environ.get(_PROVIDER_ENV_API_KEY[args.provider])
    base_url = args.base_url or os.environ.get(_PROVIDER_ENV_BASE_URL[args.provider])

    if api_key is None or api_key.strip() == "":
        env_name = _PROVIDER_ENV_API_KEY[args.provider]
        print(
            f"Provider {args.provider!r} skipped: {env_name} is not set. "
            "Pass --api-key or set the environment variable.",
            file=sys.stderr,
        )
        return 1

    config = EvaluationConfig(
        provider_name=args.provider,
        model=args.model,
        api_key=api_key,
        base_url=base_url,
        dimensions=args.dimensions,
        task=args.task,
        price_per_1m_tokens=args.price_per_1m_tokens,
        timeout_seconds=args.timeout,
    )

    if args.output is not None:
        output_path = args.output
    else:
        dataset_dir = Path(args.dataset).resolve().parent
        output_path = str(dataset_dir / "results" / f"{args.provider}-{args.model}.json")

    try:
        report = run_evaluation(
            config=config,
            dataset_path=args.dataset,
            output_path=output_path,
        )
    except ProviderSkippedError as exc:
        print(f"Provider skipped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail safely; never expose keys or tracebacks
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 2

    metrics = report.metrics
    overall = metrics.overall
    print(f"Wrote report to {output_path}")
    print(
        f"Hit@1={overall.hit_rate_at_1:.3f} Hit@3={overall.hit_rate_at_3:.3f} "
        f"Hit@5={overall.hit_rate_at_5:.3f} "
        f"Recall@1={overall.recall_at_1:.3f} Recall@3={overall.recall_at_3:.3f} "
        f"Recall@5={overall.recall_at_5:.3f} MRR={overall.mrr:.3f} "
        f"mean_latency_ms={metrics.mean_latency_ms:.1f} "
        f"p95_latency_ms={metrics.p95_latency_ms:.1f} "
        f"dimension={metrics.embedding_dimension} "
        f"failed={overall.failed_query_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
