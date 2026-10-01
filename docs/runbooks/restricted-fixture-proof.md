# Runbook — the restricted-fixture live proof (`failed_precommit` against real Odoo)

**Status: executed, 2026-09-26.** The proof passes against DEV Odoo. See "Execution record" at the end
for what was observed. The sequence is kept below as the procedure, with the one correction the
execution forced (decision 1) applied in place, so the next operator runs the version that works.

It exists because the 2.3 idempotency fix (`failed_precommit`) was proven only against a **scripted
transport**, and this project has already been burned once by exactly that: `message_post` returns a
one-element list, the scripted transport answered the `int` its author assumed, every unit test passed
and every real chatter write failed. A real refusal must be seen. See ADR 0009, the amendment to
decision 1.

## Decisions already made (do not re-litigate)

1. **Fixture:** `viewer@moni.test` — the narrowest Odoo group set that **genuinely refuses**
   `project.task.create`. Provision by **parameterising** `scripts/provision_projectread_user.py`
   (fixture name and starting groups as arguments) rather than building `projectread` and renaming
   afterwards — two half-furnished fixtures is worse than none. Keep that script's existing step that
   **probes a real `project.task.create`** instead of assuming a group name produces a refusal: the
   probe is the evidence.
   > **Amended during execution: the group set is `Role / Portal`, not `Internal User`.** The original
   > wording said "Internal User ONLY, with no `Project / User` group, so `project.task.create` is
   > refused". **That premise is false on Odoo 19.** The To-do app ships
   > `project_todo.access_task_on_partner`, granting *every* internal user full CRUD on `project.task`
   > — because To-do *is* `project.task` — and Odoo **unions** the permissions of all applicable ACL
   > rows, so an internal user without `Project / User` is still allowed to create a task. The probe
   > caught this, which is precisely why the runbook insisted it stay.
   >
   > `Role / Portal` is the narrowest set that is refused, and it is the *only* one: the groups that
   > do grant create (`Internal User`, `Project / User`) are mutually exclusive with `Portal`, since
   > `project.group_project_user` implies `base.group_user` and Odoo answers
   > `User 'Viewer' cannot be at the same time in exclusive groups 'Role / Portal', 'Internal User'`.
   > The consequence for step 4 is that the "grant" is a **group-set change** (Portal → Internal User
   > + Project / User) and the "revoke" returns to Portal. Nothing on the stand is reconfigured, and
   > the refusal is still Odoo's own ACL.
   >
   > The alternative — deactivating `project_todo.access_task_on_partner` so that "Internal User only"
   > would refuse — was rejected: it is a reversible but **stand-wide** permission change affecting
   > every internal user on DEV, made to satisfy a test. `ir.model.access` id 2840 was flipped off
   > once to confirm the diagnosis and immediately restored to `active=True`.
2. **One env name for the restricted fixture:** `MONI_ODOO_RESTRICTED_SUB` **wins** (it is already in
   `.env` and referenced in guidance). **Retire every `MONI_ODOO_TEST_SUB_3` reference** so exactly one
   name maps the restricted fixture. Two names for one fixture is how a test silently runs as the wrong
   user. Done: the name is gone from code, `.env.example` and the docs, and its `.env` value is blank.
3. **Credentials** for this procedure are passed as in-shell environment variables for **single
   commands only** — never into `.env`, code, tests, docs or `odoo_user_map`. Report where they were
   used, and rotate afterwards.

## Sequence

1. **Revert the two DEV artefacts left by earlier probing**, while admin access exists:
   - `manager@moni.test` (uid 5210) picked up custom group **589 "User"**; drop it. It could not be
     removed by the manager itself because the removal path writes
     `discuss.channel.channel_partner_ids`, which the manager may not write.
   - `project.task` **id 384** (`MONI probe explicit create (leave in place)`) — a probe artefact;
     delete it. (The no-delete rule applies to the *tools*, not to a fixture nobody wants.)
2. **Provision** `viewer@moni.test` in Odoo (starting in the refusing group set, password set) —
   parameterised script, whose defaults are now that fixture.
3. **Keycloak + map:** add the user to realm `moni` (or reuse an existing Keycloak user), then map via
   the normal flow (`scripts/remap_odoo_users.py`, `make map-odoo-user` / `list-odoo-users`) into
   `odoo_user_map`, and export its subject as `MONI_ODOO_RESTRICTED_SUB`.
4. **The live proof** in `tests/integration/odoo/test_write_tools_live.py`, extending the existing
   `create_project_task` approval-loop test:
   - run as viewer through the approval flow → **approve** → Odoo answers a real **`AccessError`**;
   - assert the `odoo_idempotency` row lands in **`failed_precommit`** and the caller still receives the
     same typed `odoo_access_error` payload a read refusal produces;
   - change the fixture's groups to the **granting** set, **retry the same key** → exactly one task is
     created, by viewer's own credentials;
   - return the groups to the **refusing** set, leaving viewer the permanent restricted fixture.
   - Put the grant/revoke rationale in the test docstring: it is what makes the second half observable,
     and it must not be "cleaned up" by a later reader.
5. **Docs:** record `viewer@moni.test` in the test-users documentation, and update the
   `MONI_ODOO_RESTRICTED_SUB` guidance (including retiring `MONI_ODOO_TEST_SUB_3`).
6. Run the five gate commands plus the DEV-Odoo suite (`pytest -m odoo tests/integration/odoo -q`), with
   the **whole** `.env` loaded into the environment — a missing `MONI_CRED_KEY` or `DATABASE_URL` fails
   in a way that looks exactly like a code bug.

## Prerequisite: cleared

`tests/unit/router/test_chat.py` **is repaired**. Its two mypy errors and five sites asserting the
superseded Phase-1 cloud contract are gone, and the repaired expectation is the one recorded here:
cloud unavailable for B/C ⇒ **degrades with `degraded=True` and the run continues locally**; a hard
`CloudUnavailable` only where the design genuinely refuses, and the one place it does — level B with
no anonymiser to hide the entities — is asserted by a test whose name says so.

Task 2.4's canary suite, its classifier/anonymiser/routing/degraded/escalation suites and ADR 0010
have also landed, so `pytest tests/unit tests/smoke` collects and passes (816 tests) and no work in
this runbook is blocked on them.

Two items were **explicitly deferred** by the task that closed 2.4, and are recorded as BACKLOG
items 5 and 6 rather than silently omitted: the single-call-site guard
(`tests/unit/router/test_cloud_gate.py`, which `policy.py` and `provider.py` currently refer to as
if it existed — it does not) and the ingest `--level` flag. Neither touches this procedure.

**One thing this procedure must not skip**, because the runbook assumes it is already true: the
fixture's own Odoo secret must be resolvable before `scripts/remap_odoo_users.py` can map it. For
`viewer` that is `ODOO_TEST_VIEWER_KEY`. The operator credential that provisions the user and changes
its groups is a *different* credential and is supplied in-shell (decision 3).

## Execution record — 2026-09-26

**Preconditions.** DEV Odoo 19.0 reached through the host port-proxy `0.0.0.0:18069 → 192.168.1.211:8069`
(TCP to the target was refused until the VPN leg came up); database `base2`. The MONI dev stack was
**down** and had to be started (`docker compose … up -d --wait`: Keycloak, Postgres, migrate, gateway,
ui, nginx, langfuse, mcp-odoo, mcp-rag, tei, redis all healthy).

**Fixture as executed.** Keycloak user `viewer` (sub `3a795272-221a-40c3-975e-85a15566c84a`, realm role
`manager`), Odoo `viewer@moni.test` uid **5215**, starting groups `Role / Portal`. Mapped into
`odoo_user_map`; `MONI_ODOO_RESTRICTED_SUB` now holds that sub (it previously held the **warehouse**
sub, which the `remap_odoo_users.py` fixture table had pointed at it for the MRP read test — viewer is
refused for MRP too, so that test still gets a genuine refusal).

**Artefacts reverted.** Group `589` (`point_of_sale.group_pos_user`, full_name "User") dropped from uid
5210 — `[24, 596, 1, 589]` → `[24, 596, 1]`. `project.task` 384 unlinked (verified `search_count` 0).
The probe added its own artefact (`project.task` 389) before the group set was corrected; it was
unlinked as its creator. Two throwaway probe tasks from the ACL investigation (390, 391) were unlinked
too. No probe litter remains.

**What the proof observed** (`pytest -m odoo tests/integration/odoo -q`): **13 passed, 1 xfailed** (the
pre-existing `res_users_apikeys.expiration_date` xfail). The router log for the new test, in order:

```
approval_requested           tool=create_project_task
approval_granted             tool=create_project_task
idempotency_claimed          model=project.task
idempotency_failed_precommit              <- Odoo answered and refused; the key is retryable
tool_error                   code=odoo_access_error   (create)
tool_error                   code=odoo_access_error   (a read refusal, same payload shape)
idempotency_reclaimed        model=project.task        <- same key, after the group-set change
idempotency_finished         odoo_id=396               <- exactly one task
```

with `create_uid == viewer (5215)` read back from Odoo, and the fixture returned to `Role / Portal`.

**Three defects the execution found, all in code that had never run against a live stand:**

1. `scripts/provision_projectread_user.py::group_names` passed the domain as `[[]]` — a domain
   containing one *empty condition* — and Odoo 19 rejects it: `Domain() invalid item in domain: []`.
   `fixture_execute_kw`'s third argument is `execute_kw`'s positional list, so `search_read` wants
   `[domain, fields]` with the domain itself first. Fixed.
2. The same script's `create_user` assumed `create` returns an `int`. Odoo 19 returns a **list** when
   it was handed a list — the sibling of the `message_post` defect ADR 0009 records. Both shapes are
   now accepted, with the live shape named in a comment. Fixed.
3. `tests/integration/odoo/test_write_tools_live.py`'s group lookup repeated defect 1 exactly (the
   domain written with one bracket too many), and its task counts were made with the **operator**
   account — which has no `project.task` access at all, so they returned a vacuous `0`. Counts and the
   `create_uid` read-back now run as the fixture, which is the account that can see the record. Fixed.

**Credential uses.** `ODOO_ADMIN_LOGIN` / `ODOO_ADMIN_PASSWORD` (an operator account, `codex.admin`,
uid 5193) were used for: dropping group 589; creating `viewer@moni.test` and setting its groups;
reading `ir.model.access` and flipping `project_todo.access_task_on_partner` (id 2840) off and back on
for the diagnosis; and, inside the live test, every group-set change the proof requires. They were
never written to code, tests, docs or `odoo_user_map`, and the delivery file was deleted at the end of
the run. `project.task` 384 was *not* deleted with them: this operator cannot read `project.task` at
all, so the manager (the record's owner) unlinked it with its own standing credential.

> **One deviation to record, because it is the failure decision 3 exists to prevent.** The pair was
> delivered as an in-shell file *outside the repository*, as approved — but during the run the two
> values also appeared in `.env`. They were not there when step 1 began, so this was an operator-side
> addition rather than something the procedure wrote. It was caught by a post-run sweep and both keys
> are now **blank** in `.env`. Decision 3 is not a formality: `.env` is read by `docker compose`, by
> `load-env.ps1` and by every host-run script, so a live admin password in it is a standing credential
> in a file that is easy to copy, back up or paste into a bug report. The check that found it is worth
> repeating whenever this runbook is executed: after the run, assert that no `ODOO_ADMIN_*` key in
> `.env` has a value.
