"""The audit row's trigger field (task 2.6, §3.8).

`trigger` answers "how did this run start" for runs no human asked for. It is a column of its own
rather than a key inside `args_redacted`, and these tests hold that line: `args_redacted` holds the
*arguments* of the thing that ran, and a reader who finds origin metadata in there has been told the
column means two things.
"""

from __future__ import annotations

from moni_gateway.audit import AuditEntry


def test_a_triggered_run_records_its_trigger() -> None:
    entry = AuditEntry(
        user_id="sub-1", action="agent.run", trigger="inbound_mail", args={"q": "hi"}
    )

    values = entry.values()

    assert values["trigger"] == "inbound_mail"
    # Anti-vacuity: the arguments are still recorded, in their own field, redacted by the same path as
    # always — the trigger did not displace them.
    assert values["args_redacted"] == {"q": "hi"}


def test_an_interactive_run_records_no_trigger() -> None:
    """NULL means "a human asked", which is why the field is nullable rather than defaulted to a
    string: a `"chat"` value would make the absence unrepresentable and tell a reader nothing."""
    values = AuditEntry(user_id="sub-1", action="chat.completion").values()

    assert values["trigger"] is None


def test_the_trigger_is_not_smuggled_into_the_arguments() -> None:
    """The two fields answer different questions, and the test asserts the *separation* rather than
    that both happen to be populated."""
    values = AuditEntry(
        user_id="sub-1", action="agent.run", trigger="inbound_mail", args=None
    ).values()

    assert values["args_redacted"] is None
    assert values["trigger"] == "inbound_mail"


def test_the_table_declares_the_trigger_column() -> None:
    """The module is the single source of truth Alembic is compared against, so a migration that adds
    the column while this table does not declare it — or the reverse — is drift. The migration and the
    live database are compared in `tests/integration/gateway/test_audit_schema.py`; this is the cheap
    half that fails without a database."""
    from moni_gateway.audit import audit_log

    assert "trigger" in {column.name for column in audit_log.columns}
    assert audit_log.c.trigger.nullable is True
