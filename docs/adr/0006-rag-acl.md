# ADR 0006 — Document RAG: the ACL lives in the query, not in the caller

- **Status:** accepted (Phase 1, task 1.5)
- **Date:** 2026-09-25
- **Deciders:** MONI AI platform

## Context

Task 1.5 adds the last piece of Phase 1: document Q&A where access is decided per document.
§3.10 states the requirement precisely — *"retrieval filters by the requesting user BEFORE
similarity search results are returned"* — and §3.12 requires that the unknown case fail
closed. Three decisions followed, and one of them is the whole point of the feature.

## Decision 1 — the ACL predicate runs in SQL, in the same statement as the ranking

Retrieval issues one query:

```sql
select c.content, c.chunk_no, s.name, 1 - (c.embedding <=> :query) as score
from doc_chunks c join doc_sources s on s.id = c.source_id
where c.acl_roles && :user_roles          -- §3.10, evaluated by Postgres
  and c.embedding is not null
order by c.embedding <=> :query           -- ranking, only over permitted rows
limit :candidates
```

Confirmed by `EXPLAIN`, which shows the predicate as the scan's own filter rather than a step
applied to the rows afterwards:

```
Seq Scan on doc_chunks c  (cost=0.00..15.00 rows=2 width=84)
      Filter: ((embedding IS NOT NULL) AND (acl_roles && '{manager}'::text[]))
```

**Why not filter in Python after the search.** It would be a *check* rather than a *filter*:
the restricted rows would already have been fetched, scored and ranked, so any later code path
that forgot the check would leak them, and the leak would be invisible because the correct
paths still looked right. Putting the predicate in the query means the restricted rows never
enter the process at all.

**Why `&&` (overlap) rather than equality.** A document readable by several roles must match a
user holding any one of them, and a user with several roles must match a document granted any
one of them. The array-overlap operator gives that symmetry, which is what lets roles be
managed independently on both sides — the alternative is keeping two lists in step, which is
exactly the kind of thing that drifts.

**Denormalised onto chunks, not joined from the source.** The filter has to run inside the same
query as the vector ranking. Joining `doc_sources` on every retrieval would work but repeats
the work per row, so `acl_roles` is copied onto each chunk at write time and
`doc_sources.acl_roles` is kept as the record of what was requested. The GIN index on
`doc_chunks.acl_roles` exists because otherwise the predicate degrades to a sequential scan of
the whole corpus.

**Verified with the strongest available test.** The integration suite crafts a director-only
document that is the *nearest vector* to a crafted manager query (deterministic fake embedder).
If the filter ran after ranking, or not at all, the manager would receive it. A pass is
therefore evidence about ordering, not merely about which document happened to rank higher.

**Deviation from the task's column list: `doc_chunks` has no `source_name` column.** The task
specifies `doc_chunks(id, source_id, source_name, chunk_no, content, embedding, acl_roles, meta,
created_at)`. Here the name is stored once, on `doc_sources`, and the ranking query projects it
as `source_name` — the citation contract is unchanged (every hit carries `source_name`, and the
system prompt requires citing it), but the copy is not stored on the chunk.

The reason is that a denormalised *name* is mutable state that nothing would keep in step.
`replace_chunks` rewrites a source's chunks when its **checksum** changes, so renaming or moving
a file would leave every existing chunk citing the old name — a stale citation is exactly the
failure the `acl_roles` assertion in `replace_chunks` exists to prevent, and there is no
equivalent assertion available for a name. `acl_roles` is denormalised for a different reason:
it is a *predicate inside the ranking query*, so it cannot be resolved after the fact, whereas
the name is a projection. The cost of the join is one primary-key lookup per candidate row over
a list the query itself bounds (25 candidates for reranking), which does not justify duplicating
a mutable string across every chunk. Recorded here rather than implemented silently, because §8
asks for a raised conflict rather than a quiet deviation.

## Decision 2 — roles travel on the identity channel, never as a tool argument

rag-mcp needs the caller's roles; odoo-mcp needs only the subject, because Odoo applies its own
ACL. The obvious move — adding a `roles` parameter to the tool — would put roles in the tool
schema, where the model can see and choose them. So they travel inside `user_context`, the one
channel that is injected by the agent from the verified token and never supplied by the model:

```
user_context = {"sub": "<keycloak sub>", "roles": ["manager", "director"]}
```

A bare subject string is still accepted and parsed as **no roles**, which is the fail-closed
reading: an old caller retrieves nothing rather than everything. Unknown role names are dropped
on both encode and decode, because a role the platform does not define cannot grant anything.

The `sub` in that payload is what §3.2 needs; the `roles` are a *narrowing* of what the token
already said. Nothing is widened by this channel — only the caller's own verified claims are in
it.

## Decision 3 — one definition of the corpus, shared by ingestion and retrieval

`moni-ingest` owns the schema, the store and the ACL-filtered search. `moni-mcp-rag` depends on
it rather than reimplementing a read path.

The reason is drift: an ingest path and a query path that embed differently — a different
normalisation, a different model, a different truncation — produce plausible-looking nonsense,
and the symptom is "the documents are not in there" rather than "the vectors are not
comparable". The same argument applies twice over to the ACL predicate, where a second
implementation would be a second chance to leak.

The cost is that the RAG image carries `pypdf`/`tiktoken` and the gateway's `db` helpers. That
is a few megabytes against a correctness property, and the alternative — duplicating the store
— is the thing that fails silently.

**One consequence worth recording:** `langgraph-checkpoint-postgres` was moved from
`moni-ingest` to `moni-agent`'s dependencies so that the RAG image does not carry a
checkpointing stack it never uses. The agent owns checkpointing; ingestion never did.

## What is deliberately absent

- **Ingesting Zoho/email history.** Phase 3 (Corporate Memory).
- **An ingestion UI, and role auto-detection from folders.** Roles are an explicit operator
  decision. Inferring them from a directory name would make a document's audience depend on
  filesystem layout, and §3.12 forbids the default-public fallback that a missing inference
  would otherwise need.
- **`pgvector` as a Python dependency.** The type is declared locally in
  `moni_ingest.schema.Vector`; all it does is emit `vector` so casts can name it. A package
  pulled into two images to render six characters is not worth the weight.
- **Caching embeddings in Redis.**

## Consequences

- **Positive:** a restricted chunk never enters the process; the guarantee is testable at the
  SQL layer (and is); ingestion is idempotent by checksum, so a re-run re-embeds nothing.
- **Negative:** `acl_roles` is duplicated between the two tables and must be updated together.
  `DocumentStore.set_acl_roles` does both in one transaction, and `replace_chunks` asserts the
  caller's roles match the source row so the two cannot silently diverge.
- **Negative:** HNSW was chosen over ivfflat because it needs no training pass — usable as soon
  as the first rows land, at the cost of a slower build and more memory. With a dev-sized
  corpus that is the right trade; a very large corpus may want the opposite.

## Alternatives considered

1. **Filter after retrieval in Python.** Rejected: a check, not a filter, and one forgotten
   call site from leaking.
2. **`roles` as a tool parameter.** Rejected: puts authorization inside the model's reach.
3. **A separate `check` function called by the tool before returning.** Rejected: two places
   must agree, and the failure mode is silent.
4. **Duplicating the store in rag-mcp.** Rejected: two chance to diverge on both the embedding
   and the predicate.
5. **One row per document with the ACL, joining at query time.** Rejected: repeats the join on
   every retrieval for no gain, and the filter must be in the ranking query either way.
