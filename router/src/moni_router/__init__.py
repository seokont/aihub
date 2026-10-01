"""MONI AI LLM Router.

Owns every model call: level A stays on the local vLLM, level B is anonymised
before it may leave the server, level C may use the configured cloud provider
(CLAUDE.md §3.4). Policy will live in ``moni_router/policy.py`` and nowhere else.

Task 0.1 scope: package skeleton only. No providers, no business logic.
"""
