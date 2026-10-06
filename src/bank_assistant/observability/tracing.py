"""OpenTelemetry tracing setup. Spans carry ids, sizes, models, token counts and outcomes, never message
text, so traces don't become a second copy of customer data."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

_configured = False


def setup_tracing(service_name: str, otlp_endpoint: str | None = None,
                  extra_processor: SpanProcessor | None = None) -> None:
    """Install a tracer provider once per process. With no endpoint, spans are created (so code paths and
    tests behave the same) but not exported."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter: SpanExporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    if extra_processor is not None:
        provider.add_span_processor(extra_processor)
    trace.set_tracer_provider(provider)
    _configured = True
