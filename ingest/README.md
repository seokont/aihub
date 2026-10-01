# moni-ingest — document ingestion with per-document ACLs

Loads documents, extracts their text, chunks it, embeds it through TEI, and writes the result
into pgvector with an explicit audience (CLAUDE.md §3.10). It also owns `DocumentStore`, the
read path that rag-mcp uses, so ingestion and retrieval share one definition of the schema and
of the ACL predicate.

## Ingesting

```bash
uv run --group dev python -m moni_ingest add ./docs/contracts --roles manager,director --level B
uv run --group dev python -m moni_ingest list
uv run --group dev python -m moni_ingest search "умови оплати" --roles manager
```

`--roles` is **required**. There is no default and no `--public` flag: a document whose
audience was not stated is a document nobody can retrieve, and inventing an audience on the
operator's behalf is exactly the fail-open behaviour §3.12 forbids. Role names are validated
against the realm's roles, so a typo fails immediately rather than creating a document no real
user can match.

`--level` is the opposite case and **defaults to `A`**. The *audience* is something only a human
knows; the *level* has a safe answer when nobody says, and the safe answer is the most restrictive
one (§3.12). The two flags are the same rule applied to failure modes that differ — an unstated
audience produces a document nobody can retrieve, which is loud, while an unstated level produces
one that looks fine and may leak. Unlike `--roles` there is therefore no reason to make it required:
an operator who did not know the level would have to guess, and would guess in whichever direction
the help text leaned.

The level is a property of the **document**, not of the retrieval: a salary table is A whoever asks
for it, a public price list is C. It is written onto every chunk, returned with each retrieved
passage, and read by the router's classifier to decide whether that context may leave the server.
An unrecognised level is refused rather than coerced — see `pipeline.validate_level` for why that is
not the same decision as the A default.

Re-running is safe and cheap. The checksum is taken over the *extracted text*, so a document
whose content is unchanged is skipped entirely — no re-chunking, no re-embedding, no new rows —
and an unchanged PDF re-saved with a new timestamp is still unchanged. When the text does
change, the source's chunks are replaced wholesale in one transaction, so a shorter document
cannot leave its former tail behind. A re-run **does** restate the roles and the level, because
those are properties the operator states rather than consequences of the text: re-ingesting with a
new `--level` moves it without re-embedding anything.

Supported formats: **PDF** (pypdf, falling back to pdfplumber when pypdf extracts nothing),
**DOCX** (paragraphs and table cells), **Markdown** and **plain text**. Anything else is
refused rather than guessed at.

## How text is chunked

~**800 tokens** per chunk with **120** of overlap, counted with `tiktoken`'s `o200k_base`.

Not the bge-m3 tokenizer, deliberately: that would mean downloading a multi-GB model to count
tokens, while chunking only needs a *stable, dense* notion of "about 800 tokens". Both the
window and the overlap use the same tokenizer, so they stay consistent with each other — which
is what the boundary behaviour actually depends on. Chunking is deterministic: the same text
always produces byte-identical chunks, which is what makes the checksum a meaningful change
key.

## Configuration

| Variable | Purpose |
| --- | --- |
| `EMBEDDINGS_BASE_URL` | TEI endpoint, **host**-facing (e.g. `http://127.0.0.1:8082/v1`) |
| `EMBEDDINGS_MODEL` | model id reported by TEI (`BAAI/bge-m3`) |
| `DATABASE_URL` | the corpus lives in the main PostgreSQL, alongside the audit trail |

Vectors are asserted to be 1024-dimensional because the column is `vector(1024)`. A mismatch is
reported as such rather than surfacing later as a Postgres type error.

## Layout

| Module | Responsibility |
| --- | --- |
| `extract` | one function per format; raises rather than returning empty text |
| `chunking` | deterministic token windows with overlap; the checksum |
| `embeddings` | the `Embedder` protocol and the TEI client |
| `schema` | the `doc_sources` / `doc_chunks` definitions (migration `0004` creates them) |
| `store` | upsert by checksum, replace chunks, **the ACL-filtered search** |
| `pipeline` | extract → checksum → chunk → embed → upsert, drivable from tests |
| `cli` | `add`, `list`, `search` |

## Not here yet

Corporate Memory ingestion (Zoho mail history) is Phase 3. There is no ingestion UI, roles are
never inferred from folder names, and embeddings are not cached in Redis — all deliberate, see
`docs/adr/0006-rag-acl.md`.
