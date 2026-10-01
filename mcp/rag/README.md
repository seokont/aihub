# moni-rag-mcp — ACL-filtered document retrieval

One MCP server on `127.0.0.1:8012`, exposing one tool:

| Tool | Action class | Returns |
| --- | --- | --- |
| `search_documents(query, top_k<=8)` | `read` | passages with `source_name`, `chunk_no`, `score`, `level` |

Deliberately the only tool. Retrieval is what Phase 1 needs, and a "list all documents" tool
would create a second path to the same data that a future ACL change could miss.

`level` is the classification fixed for that chunk **at ingest time** (§3.4) — `A`, `B` or `C`. It is
carried in the payload because the router has to classify the context it is about to send, and this
is the one part of that classification the store knows for certain. A retrieval that dropped it
would make every document look undeclared, which composes to `A`: fail-closed, and a silent quality
loss rather than a fault anybody would trace back to this field. The reranker is the place it gets
dropped in practice — it rebuilds each hit rather than reusing it, so every field it does not copy
it loses.

## The ACL is enforced in SQL, before ranking

This is the property §3.10 is about, and it is not implemented here — it is implemented in
`moni_ingest.store.DocumentStore.search`, which this server calls:

```sql
where c.acl_roles && :user_roles     -- the caller's roles, from the verified token
order by c.embedding <=> :query      -- ranking, already restricted
```

`EXPLAIN` confirms the predicate is the scan's own filter rather than a step applied to the
results:

```
Seq Scan on doc_chunks c  (cost=0.00..15.00 rows=2 width=84)
      Filter: ((embedding IS NOT NULL) AND (acl_roles && '{manager}'::text[]))
```

Nothing in this package filters results in Python. By the time a row reaches here, Postgres has
already decided the caller may read it — which is the difference between a *filter* and a
*check*: a check can be forgotten by a later code path, a filter cannot.

The `&&` operator is array overlap, so a document granted several roles matches a user holding
any one of them, and a user with several roles matches a document granted any one of them.

## Identity

Every tool takes `user_context` first; the agent injects it from the verified JWT and the model
never sees or supplies it. For retrieval it carries the roles as well as the subject:

```json
{"sub": "<keycloak sub>", "roles": ["manager", "director"]}
```

A bare subject string is also accepted and parsed as **no roles**, which is the fail-closed
reading: an older caller retrieves nothing rather than everything (§3.12). Unknown role names
are dropped, since a role the platform does not define cannot grant anything.

Roles are *not* a tool argument, which would put them inside the model's reach. See
`moni_mcp_rag/identity.py`.

## Reranking is optional

With `RERANKER_BASE_URL` unset the search returns the vector order — a supported state, not a
degraded one. With it set, the top ~25 candidates are reordered by a TEI cross-encoder.

A configured reranker that **fails** is not silently skipped: the payload reports
`"reranked": false` and the failure is logged, because a retrieval that quietly returns worse
ordering is a quality problem nobody notices. Either way, reranking changes only the *order* —
never which documents a user may see, since the ACL was applied before the candidates existed.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | the corpus (same database as the audit trail) |
| `EMBEDDINGS_BASE_URL` | TEI endpoint, **container**-facing (`http://host.docker.internal:18082/v1`) |
| `EMBEDDINGS_MODEL` | `BAAI/bge-m3` |
| `RERANKER_BASE_URL` | optional TEI cross-encoder |
| `MONI_MCP_RAG_HOST` / `_PORT` | listen address; `0.0.0.0:8012` in the container |

The port is published on `127.0.0.1` only (§3.1) and exists for developer probes — the gateway
reaches this server over the compose network, and the UI never calls an MCP server.

## Checking it without the UI

The store's search is reachable from the ingestion CLI, which is the quickest way to confirm
what a given set of roles can see:

```bash
uv run --group dev python -m moni_ingest search "умови оплати" --roles manager
uv run --group dev python -m moni_ingest search "умови оплати" --roles director
```

A document appearing for `director` and not for `manager` is the access model working.
