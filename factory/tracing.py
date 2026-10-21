"""Arize AX tracing for the factory.

Trace shape (one GitHub issue = one trace):

    factory.run            CHAIN   root; input = issue, output = PR url
      station.spec         CHAIN   one per station; metadata.station = "spec"
        <AGENT span>               from ClaudeAgentSDKInstrumentor, with TOOL children
      station.build        CHAIN
        <AGENT span>
      station.ci           CHAIN   waits on GitHub Actions (no agent)
      station.babysit      CHAIN   only when CI fails; AGENT child
      station.review       CHAIN
        station.review_correctness / _security / _tests   CHAIN, AGENT child each (parallel)
      station.router       CHAIN   AGENT child + rules floor
      station.automerge | station.human
      station.release      CHAIN   AGENT child (smoke test + notes), then the tag
    factory.resume         CHAIN   child of factory.run, when --watch picks up a human verdict

Every span in the run carries session.id = "<repo>#<issue>", so reruns and babysit
loops for the same issue group as one AX session. Station spans carry the metadata
the outcome harvester and evals join on: station, issue, pr, attempt. Agent spans also
carry the Prompt Hub version that ran (prompt_name, prompt_version_id, prompt_source).

With no Arize credentials, init() still installs a tracer (so trace ids exist for the
dashboard) but nothing is exported.
"""
from __future__ import annotations

import json
import logging
import os

# gRPC's fork handlers deadlock when the Agent SDK spawns several `claude` subprocesses
# at once (the three parallel reviewers): the forked child hangs in grpc_postfork_child
# before exec, and the parent's event loop blocks waiting for it. We export over HTTP
# so gRPC is never on the fork path; this is a second guard in case something imports it.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace

from . import config

log = logging.getLogger(__name__)
_provider: Any = None
_tracer: Any = None


def init() -> None:
    global _provider, _tracer
    if _tracer is not None:
        return
    from openinference.instrumentation import OITracer, TraceConfig
    from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor

    if config.ARIZE_SPACE_ID and config.ARIZE_API_KEY:
        # Built by hand rather than arize.otel.register(): register() always formats a
        # details banner that crashes for HTTP exporters in arize-otel 0.13.
        from arize.otel import HTTPSpanExporter
        from openinference.semconv.resource import ResourceAttributes
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create(
            {ResourceAttributes.PROJECT_NAME: config.ARIZE_PROJECT_NAME}))
        _provider.add_span_processor(BatchSpanProcessor(HTTPSpanExporter(
            space_id=config.ARIZE_SPACE_ID, api_key=config.ARIZE_API_KEY,
            endpoint="https://otlp.arize.com/v1/traces")))
        trace.set_tracer_provider(_provider)
        log.info("Tracing to Arize project %s", config.ARIZE_PROJECT_NAME)
    else:
        from opentelemetry.sdk.trace import TracerProvider

        _provider = TracerProvider()
        trace.set_tracer_provider(_provider)
        log.warning("ARIZE_SPACE_ID/ARIZE_API_KEY not set: running untraced")

    ClaudeAgentSDKInstrumentor().instrument(tracer_provider=_provider)
    _tracer = OITracer(_provider.get_tracer("software-factory"), config=TraceConfig())


def shutdown() -> None:
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()


def _flat(meta: dict) -> str:
    return json.dumps({k: v for k, v in meta.items() if v is not None})


@contextmanager
def span(name: str, *, session_id: str, metadata: dict, input: Any = None,
         parent: tuple[str, str] | None = None) -> Iterator[Any]:
    """A CHAIN span with session + metadata set the OpenInference way.

    parent=(trace_id, span_id) attaches the span to an earlier trace, so a run resumed
    after the human gate (in another process, maybe hours later) stays one trace.
    """
    attrs = {"session.id": session_id, "metadata": _flat(metadata)}
    for k, v in metadata.items():
        if isinstance(v, (str, int, float, bool)):
            attrs[f"factory.{k}"] = v
    if input is not None:
        attrs["input.value"] = input if isinstance(input, str) else json.dumps(input)
        attrs["input.mime_type"] = "text/plain" if isinstance(input, str) else "application/json"
    ctx = None
    if parent:
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        sc = SpanContext(trace_id=int(parent[0], 16), span_id=int(parent[1], 16),
                         is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
        ctx = trace.set_span_in_context(NonRecordingSpan(sc))
    with _tracer.start_as_current_span(name, context=ctx, attributes=attrs,
                                       openinference_span_kind="chain") as s:
        yield s


def set_output(s: Any, value: Any) -> None:
    if isinstance(value, str):
        s.set_attribute("output.value", value)
        s.set_attribute("output.mime_type", "text/plain")
    else:
        s.set_attribute("output.value", json.dumps(value))
        s.set_attribute("output.mime_type", "application/json")


def trace_id_of(s: Any) -> str:
    return format(s.get_span_context().trace_id, "032x")


def span_id_of(s: Any) -> str:
    return format(s.get_span_context().span_id, "016x")


def trace_url(trace_id: str, start_ms: int, end_ms: int) -> str | None:
    """Arize UI deep link for a trace, if ARIZE_ORG_ID and ARIZE_PROJECT_ID are set."""
    if not (config.ARIZE_ORG_ID and config.ARIZE_PROJECT_ID and config.ARIZE_SPACE_ID):
        return None
    return (
        f"https://app.arize.com/organizations/{config.ARIZE_ORG_ID}/spaces/{config.ARIZE_SPACE_ID}"
        f"/projects/{config.ARIZE_PROJECT_ID}?selectedTraceId={trace_id}&queryFilterA="
        f"&selectedTab=llmTracing&timeZoneA=America%2FLos_Angeles&startA={start_ms}"
        f"&endA={end_ms}&envA=tracing&modelType=generative_llm"
    )
