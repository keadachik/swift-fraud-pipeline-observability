"""
Phoenix tracing setup for the SWIFT agent pipeline.

Prerequisites (separate terminal):
    phoenix serve
UI: http://localhost:6006

This module:
1. Registers Phoenix as the OpenTelemetry collector
2. Auto-instruments the OpenAI SDK (LLM spans)
3. Exposes helpers for manual chain / agent spans around pipeline stages
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

_TRACING_READY = False
_TRACER = None


def setup_tracing(
    project_name: str = "swift-workflow",
    endpoint: Optional[str] = None,
) -> Any:
    """
    Initialize Phoenix OpenTelemetry export + OpenAI auto-instrumentation.

    Call once at process start, before creating OpenAI clients / agents.
    Returns the tracer provider (or None if packages are missing).
    """
    global _TRACING_READY, _TRACER

    if _TRACING_READY:
        return _TRACER

    try:
        from phoenix.otel import register
    except ImportError as exc:
        raise ImportError(
            "Phoenix tracing packages missing. Install with:\n"
            '  pip install "arize-phoenix" "arize-phoenix-otel" '
            '"openinference-instrumentation-openai"'
        ) from exc

    register_kwargs: Dict[str, Any] = {
        "project_name": project_name,
        "auto_instrument": True,
        "batch": True,
    }
    if endpoint:
        register_kwargs["endpoint"] = endpoint

    tracer_provider = register(**register_kwargs)

    from opentelemetry import trace

    _TRACER = trace.get_tracer("swift-workflow")
    _TRACING_READY = True
    print(
        f"Phoenix tracing enabled → project={project_name!r} "
        "(UI usually http://localhost:6006)"
    )
    return tracer_provider


def _get_tracer():
    if _TRACER is None:
        from opentelemetry import trace

        return trace.get_tracer("swift-workflow")
    return _TRACER


def _safe_json(value: Any, limit: int = 4000) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


@contextmanager
def traced_stage(
    name: str,
    *,
    kind: str = "CHAIN",
    input_value: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """
    Manual span for a pipeline stage. OpenAI calls inside nest as children.

    kind: CHAIN | AGENT | TOOL | RETRIEVER (OpenInference span kinds)
    """
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    kind_value = getattr(OpenInferenceSpanKindValues, kind, OpenInferenceSpanKindValues.CHAIN).value
    tracer = _get_tracer()

    with tracer.start_as_current_span(name) as span:
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind_value)
        if input_value is not None:
            span.set_attribute(SpanAttributes.INPUT_VALUE, _safe_json(input_value))
        if metadata:
            for key, value in metadata.items():
                attr = value if isinstance(value, (str, int, float, bool)) else _safe_json(value)
                span.set_attribute(f"swift.{key}", attr)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            raise


def set_span_output(span: Any, output_value: Any) -> None:
    """Attach a truncated JSON output to the current manual span."""
    if span is None:
        return
    from openinference.semconv.trace import SpanAttributes

    span.set_attribute(SpanAttributes.OUTPUT_VALUE, _safe_json(output_value))
