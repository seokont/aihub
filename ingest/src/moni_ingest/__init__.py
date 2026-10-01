"""MONI AI ingestion pipelines.

Owns document loading, chunking, embedding via the external TEI endpoint, and ACL-tagged
writes into pgvector (CLAUDE.md §3.10). Corporate Memory ingestion (Zoho history) is Phase 3
and does not live here yet.

Entry points:

* :mod:`moni_ingest.cli` — ``python -m moni_ingest`` for operators;
* :func:`moni_ingest.pipeline.ingest_file` — the same run, drivable from tests with a fake
  embedder;
* :class:`moni_ingest.store.DocumentStore` — the read path shared with rag-mcp, so ingestion
  and retrieval cannot disagree about the schema or the ACL predicate.

Every chunk carries its own ``acl_roles``; retrieval filters on it **in SQL**, before ranking.
"""

__all__: list[str] = []
