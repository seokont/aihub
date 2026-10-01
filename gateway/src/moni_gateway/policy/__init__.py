"""``moni_gateway.policy`` — what a tool *is*, and whether a caller may run it.

Two modules, deliberately separate:

* :mod:`moni_gateway.policy.registry` — classification. One reviewed mapping from tool name to
  ``read`` / ``write`` / ``irreversible`` (CLAUDE.md §3.3).
* :mod:`moni_gateway.policy.engine` — authorization. A pure decision over the caller, the tool and
  the class: ``allow`` / ``require_approval`` / ``deny``.

Neither imports FastAPI, the database or the agent. Policy that has to be *constructed* is policy
that is hard to test exhaustively, and §3.3 is the one rule in the specification that most needs an
exhaustive test.
"""
