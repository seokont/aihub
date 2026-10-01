"""rag-mcp — retrieval over the vector store.

Read-only by construction: it queries pgvector and returns chunks the requesting
user is allowed to see, honouring the per-chunk ACL (CLAUDE.md §3.10).

Task 0.1 scope: package skeleton only. No retrieval, no client.
"""
