"""Integration tests — require the dev stack from infra/docker-compose.dev.yml.

Nothing here yet. The first integration test belongs in the task that introduces a
DB-backed path (Phase 1). Until then the gateway's identity boundary is covered by
tests/unit/gateway/, which needs no containers.

When tests land here:

* mark them ``@pytest.mark.integration`` (registered in the root pyproject.toml);
* point them at a DEV/staging instance — never production (CLAUDE.md §3.9);
* keep the secrets they need in ``.env``, never in test code (§3.11).
"""
