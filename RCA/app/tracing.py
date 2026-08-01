import logging

from app.settings import get_settings

logger = logging.getLogger("rca_agent.tracing")

_client = None


def get_langfuse():
    global _client
    settings = get_settings()
    if _client is None and settings.langfuse_configured:
        from langfuse import Langfuse

        kwargs = {"public_key": settings.langfuse_public_key, "secret_key": settings.langfuse_secret_key}
        if settings.langfuse_base_url:
            kwargs["base_url"] = settings.langfuse_base_url
        _client = Langfuse(**kwargs)
    return _client


def traced(name: str):
    """Wraps a ladder stage in a Langfuse span. No-op when Langfuse isn't configured, so
    the investigation never depends on tracing credentials to run locally or in tests —
    ponytail: env-gated like clickhouse_client's lazy singleton, not a hard dependency."""
    if not get_settings().langfuse_configured:
        return lambda fn: fn

    from langfuse import observe

    return observe(name=name)


def record_query(sql: str, parameters, query_id, read_rows, elapsed_s: float) -> None:
    """Attaches SQL text + query_id + row/elapsed stats to whichever @traced span is
    currently active — the evidence that ClickHouse, not the LLM, did the work."""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.update_current_span(
            metadata={
                "sql": sql,
                "parameters": parameters,
                "query_id": query_id,
                "read_rows": read_rows,
                "elapsed_s": elapsed_s,
            }
        )
    except Exception:
        logger.warning("failed to record query span", exc_info=True)


def flush() -> None:
    """Blocks until the trace is sent — an unflushed trace on a short-lived process is
    worth zero on the 'no trace, no credit' criterion."""
    client = get_langfuse()
    if client is not None:
        client.flush()
