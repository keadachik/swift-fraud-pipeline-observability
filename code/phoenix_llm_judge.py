"""
Arize Phoenix-style LLM-as-a-Judge:

1. Produce a traced agent run (spans land in Phoenix)
2. Pull those spans from Phoenix
3. Run phoenix.evals ClassificationEvaluator (LLM judge)
4. log_span_annotations_dataframe → labels appear on the Trace in the UI

This is the course workflow — not pytest-only terminal output.

Usage (Phoenix must already be running: `phoenix serve`):

    cd project
    uv run python -m evals.phoenix_llm_judge
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from phoenix.client import Client
from phoenix.evals import ClassificationEvaluator, evaluate_dataframe
from phoenix.evals.llm import LLM
from phoenix.evals.utils import to_annotation_dataframe

from agents.prompt_chaining import PromptChainingPattern
from config import Config
from observability.phoenix_tracing import set_span_output, setup_tracing, traced_stage

PROJECT_NAME = "swift-workflow"


def _sample_message() -> Dict[str, Any]:
    return {
        "message_id": "PHOENIX-JUDGE-001",
        "message_type": "MT103",
        "reference": "REF123456789012",
        "amount": "12500.00 USD",
        "sender_bic": "CHASUS33XXX",
        "receiver_bic": "BARCGB22XXX",
        "remittance_info": "Supplier invoice Q4",
    }

GROUNDING_TEMPLATE = """
You are an evaluation judge for a SWIFT fraud review system.
Decide if the analyst justification is grounded in the provided evidence only.

Definitions:
- grounded: All material claims are supported by the SWIFT message and/or prior analysis summary.
- not_grounded: Invents or contradicts facts (wrong BIC, amount, country, flags, etc.).

[BEGIN DATA]
************
[Message]: {message_json}
************
[Decision]: {decision}
************
[Justification]: {justification}
************
[Prior analysis summary]: {prior_summary}
[END DATA]

Respond with a single word label: grounded or not_grounded.
"""

CLARITY_TEMPLATE = """
You are an evaluation judge.
Decide if the fraud decision justification is clear to a human reviewer.

Definitions:
- clear: States the decision rationale in understandable language with concrete factors.
- unclear: Vague, contradictory, or missing why the decision was made.

[BEGIN DATA]
************
[Decision]: {decision}
************
[Justification]: {justification}
[END DATA]

Respond with a single word label: clear or unclear.
"""


def _judge_llm() -> LLM:
    cfg = Config()
    kwargs: Dict[str, Any] = {"api_key": cfg.OPENAI_API_KEY}
    if getattr(cfg, "OPENAI_BASE_URL", ""):
        kwargs["base_url"] = cfg.OPENAI_BASE_URL
    return LLM(provider="openai", model="gpt-4o", **kwargs)


def run_traced_chain_for_eval(message: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    One Prompt Chaining run under Phoenix tracing.
    Stores decision/justification on the span for later eval.
    """
    message = message or _sample_message()

    with traced_stage(
        "prompt_chaining_for_judge",
        kind="AGENT",
        input_value=message,
        metadata={"purpose": "phoenix_llm_judge"},
    ) as span:
        chain = PromptChainingPattern()
        results = chain.process_chain([message])
        final = results.get("final_review") or {}
        decisions = final.get("final_decisions") or []
        row = next(
            (d for d in decisions if d.get("message_id") == message["message_id"]),
            decisions[0] if decisions else {},
        )
        payload = {
            "message": message,
            "decision": row.get("decision"),
            "justification": row.get("justification"),
            "prior_summary": {
                "initial_screening": results.get("initial_screening"),
                "technical_analysis": results.get("technical_analysis"),
            },
        }
        set_span_output(span, payload)
        # Also set flat attributes some UIs surface more easily
        if row.get("decision"):
            span.set_attribute("swift.decision", str(row["decision"]))
        if row.get("justification"):
            span.set_attribute("swift.justification", str(row["justification"])[:2000])
        return payload


def _parse_span_output(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def fetch_judge_candidate_spans(
    project_name: str = PROJECT_NAME,
    limit: int = 50,
) -> pd.DataFrame:
    """Pull recent judge-target spans and flatten fields for phoenix.evals."""
    client = Client()
    spans_df = client.spans.get_spans_dataframe(
        project_name=project_name,
        limit=limit,
    )
    if spans_df is None or spans_df.empty:
        raise RuntimeError(
            f"No spans in Phoenix project {project_name!r}. "
            "Run a traced pipeline first (this script does that)."
        )

    # Prefer our dedicated span name; fall back to prompt_chaining
    name_col = "name" if "name" in spans_df.columns else None
    if name_col:
        targeted = spans_df[spans_df[name_col] == "prompt_chaining_for_judge"]
        if targeted.empty:
            targeted = spans_df[spans_df[name_col] == "prompt_chaining"]
        if not targeted.empty:
            spans_df = targeted

    rows: List[Dict[str, Any]] = []
    for idx, row in spans_df.iterrows():
        span_id = row.get("context.span_id") or (idx if isinstance(idx, str) else None)
        if span_id is None and hasattr(idx, "__iter__"):
            # MultiIndex fallback
            span_id = row.get("context.span_id")
        out = _parse_span_output(row.get("attributes.output.value"))
        if not out.get("justification") and not out.get("decision"):
            continue
        rows.append(
            {
                "context.span_id": span_id,
                "message_json": json.dumps(out.get("message") or {}, default=str),
                "decision": out.get("decision") or "",
                "justification": out.get("justification") or "",
                "prior_summary": json.dumps(out.get("prior_summary") or {}, default=str)[:4000],
            }
        )

    if not rows:
        raise RuntimeError(
            "Found spans but none had decision/justification in attributes.output.value. "
            "Re-run this script so prompt_chaining_for_judge writes that payload."
        )

    eval_df = pd.DataFrame(rows)
    eval_df = eval_df.dropna(subset=["context.span_id"])
    eval_df = eval_df.set_index("context.span_id")
    return eval_df


def run_judges_and_log_to_phoenix(eval_df: pd.DataFrame) -> pd.DataFrame:
    """phoenix.evals ClassificationEvaluator → annotations on spans."""
    llm = _judge_llm()
    grounding = ClassificationEvaluator(
        name="final_review_grounding",
        prompt_template=GROUNDING_TEMPLATE,
        llm=llm,
        choices={"grounded": 1.0, "not_grounded": 0.0},
        include_explanation=True,
    )
    clarity = ClassificationEvaluator(
        name="final_review_clarity",
        prompt_template=CLARITY_TEMPLATE,
        llm=llm,
        choices={"clear": 1.0, "unclear": 0.0},
        include_explanation=True,
    )

    results_df = evaluate_dataframe(
        dataframe=eval_df,
        evaluators=[grounding, clarity],
    )
    annotations_df = to_annotation_dataframe(dataframe=results_df)

    client = Client()
    # Log each evaluator name if the util already expanded rows; otherwise log once.
    if "annotation_name" in annotations_df.columns or "name" in annotations_df.columns:
        client.spans.log_span_annotations_dataframe(
            dataframe=annotations_df,
            annotator_kind="LLM",
            sync=True,
        )
    else:
        # Fallback: log grounding/clarity separately if columns are prefixed
        client.spans.log_span_annotations_dataframe(
            dataframe=annotations_df,
            annotator_kind="LLM",
            sync=True,
        )

    return results_df


def main() -> None:
    print("1) Register tracing → Phoenix project", PROJECT_NAME)
    setup_tracing(project_name=PROJECT_NAME)

    print("2) Run traced Prompt Chaining (creates spans)")
    payload = run_traced_chain_for_eval()
    print("   decision:", payload.get("decision"))
    print("   justification:", (payload.get("justification") or "")[:200], "…")

    # Give the collector a moment to flush batch spans
    print("3) Flush / wait for spans")
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=10000)
    except Exception as exc:
        print("   flush note:", exc)
    time.sleep(2)

    print("4) Fetch spans from Phoenix")
    eval_df = fetch_judge_candidate_spans(PROJECT_NAME)
    print(f"   rows to judge: {len(eval_df)}")

    print("5) Run phoenix.evals LLM judges + log annotations")
    results_df = run_judges_and_log_to_phoenix(eval_df)
    print(results_df.to_string())

    print(
        "\nDone. Open http://localhost:6006 → project "
        f"{PROJECT_NAME} → open the latest trace → look for Evaluations / "
        "Annotations: final_review_grounding, final_review_clarity"
    )


if __name__ == "__main__":
    main()
