"""MONI AI Agent Core.

Will host the LangGraph graphs, prompts, verification and hard limits
(max 20 steps per run, max 2 retries per step — CLAUDE.md §3.6) plus the
``interrupt()`` approval points.

Task 0.1 scope: package skeleton only. No graphs, no business logic.
"""
