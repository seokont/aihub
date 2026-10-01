#!/usr/bin/env python3
"""Provision a restricted DEV fixture user: Keycloak ``manager`` role + under-privileged Odoo user.

    # the restricted fixture the `failed_precommit` live proof needs (the default)
    uv run --group dev python scripts/provision_projectread_user.py --dry-run
    uv run --group dev python scripts/provision_projectread_user.py

    # the earlier, less restricted fixture: project/task read but no create
    uv run --group dev python scripts/provision_projectread_user.py \\
        --fixture projectread --groups "Internal User,Project / User"

**What this fixture is for, and why it needs its own user.** ADR 0009's amendment makes a key
``failed_precommit`` — and therefore retryable — when Odoo *answers and refuses* a create. That
behaviour was, until this fixture, proved only through a **scripted transport**: a test double that
answers ``AccessError``. This repository has already been burned by exactly that shape of proof
(``message_post`` returns a one-element list; the scripted transport answered the ``int`` its author
assumed, every unit test passed and every real chatter write failed). So an under-privileged real
Odoo user is what turns "the ledger marks this row retryable" from a claim about a fake into a fact
about DEV Odoo.

The fixture's two halves are deliberately different:

* **Keycloak** — a user with the realm role ``manager``, which is the role RBAC offers the write
  tools to. Without a write tool in the tool list the approval loop cannot be driven at all, so the
  *Keycloak* half must be permissive;
* **Odoo** — the *same* login, whose groups **exclude** the grant that lets ``project.task`` be
  created. Odoo's ACL is the final word on a write (§3.2), which is what makes this user's refusal a
  *real* refusal.

**Why the restricted fixture is a *Portal* user, and not "Internal User only".** The runbook that
owns this procedure first specified ``Internal User`` only, on the assumption that an internal user
without ``Project / User`` cannot create a task. **That assumption is false on Odoo 19.** The To-do
app ships ``project_todo.access_task_on_partner``, which grants *every* internal user full CRUD on
``project.task`` — because To-do *is* ``project.task`` — and Odoo unions the permissions of all
applicable ACL rows, so an internal user is allowed to create one. The probe below is what
established this rather than a group name being trusted, which is exactly why the runbook insisted
the probe stay. ``Role / Portal`` is therefore the narrowest group set that is genuinely refused,
and it is the only one: the groups that *do* grant create (``Internal User``, ``Project / User``)
are mutually exclusive with ``Portal``, because ``project.group_project_user`` implies
``base.group_user`` and Odoo refuses ``User cannot be at the same time in exclusive groups``.

The consequence for the live proof is recorded with it: the fixture starts **Portal only** (refused),
the "grant" step switches it to ``Internal User`` + ``Project / User`` (allowed), and the revoke
returns it to ``Portal``. Nothing on the stand is reconfigured — the flip is group membership, which
is what makes the second half of ADR 0009's amendment observable.

**Parameterised, because there is more than one such fixture and building one to rename it later is
not the same thing.** The fixture name, the Odoo group set it starts in, the realm role and the
``.env`` variable its subject is written to are all arguments. The projectread fixture (``Internal
User`` + ``Project / User``) and the restricted one the live proof needs (``Role / Portal``) differ
*only* in that group list — so they are one script and two invocations rather than two scripts that
drift apart. The defaults are the **restricted** fixture, because that is the one the procedure in
``docs/runbooks/restricted-fixture-proof.md`` provisions: ``viewer@moni.test``, ``Role / Portal``,
subject written to ``MONI_ODOO_RESTRICTED_SUB``.

**Idempotent by construction.** Re-running creates nothing twice: the Keycloak user is looked up by
exact username first, the Odoo user by exact login, and the group set is *asserted* rather than
appended to. Every step prints ``exists`` or ``created``/``updated`` so a rerun is comparable with the
first run.

**Where the credentials come from, and where they must not go.** Two different credentials, two
different rules:

* **the fixture's own Odoo secret** (``--credential-var``, default ``ODOO_TEST_VIEWER_KEY``) is a
  standing DEV fixture credential, the same shape ``scripts/remap_odoo_users.py`` uses for the
  manager and warehouse fixtures. On this DEV instance it may be the user's *password* rather than an
  API key: ``base2``'s ``res_users_apikeys`` schema is incomplete (see README known issues), and Odoo
  accepts either in the same position;
* **the operator credential** that may write ``res.users``/``res.groups`` is supplied as an
  **in-shell environment variable for single commands** and is never written to ``.env``, code, docs
  or ``odoo_user_map`` (runbook decision 3). It is looked up in the process environment **first**
  and only falls back to ``.env`` so an operator who already filled the older placeholders is not
  broken. The mapped ``manager@moni.test`` cannot write ``res.users``, which is why a dedicated
  operator account is required at all.

**What it does when it cannot proceed.** Absent an operator credential the script says so and stops
**after** the Keycloak half, rather than pretending the fixture is complete.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_ENV = REPO_ROOT / ".env"
for _path in (REPO_ROOT / "gateway" / "src", REPO_ROOT / "mcp" / "odoo" / "src"):
    sys.path.insert(0, str(_path))

#: The realm role that makes RBAC offer the write tools. ``manager`` rather than ``admin`` on
#: purpose: this user is meant to be an ordinary employee whose *Odoo* rights are narrow, and the
#: interesting question is what Odoo does — not what an admin role would have papered over.
DEFAULT_REALM_ROLE = "manager"

#: The **restricted** fixture's default group set: a Portal user and nothing else.
#:
#: Not ``Internal User``, and the reason is a live finding rather than a preference. On Odoo 19 the
#: To-do app ships an ACL row granting *every* internal user full CRUD on ``project.task`` (To-do
#: *is* ``project.task``), and Odoo unions the permissions of all applicable rows — so an internal
#: user without ``Project / User`` is still allowed to create a task, and cannot demonstrate the
#: refusal this fixture exists for. ``Role / Portal`` is the narrowest set that *is* refused, and the
#: granting groups are mutually exclusive with it (``project.group_project_user`` implies
#: ``base.group_user``, and Odoo rejects a user holding both ``Role / Portal`` and ``Internal User``).
#:
#: Whether a given group set actually refuses is the one thing this script must not assume — Odoo's
#: ACL tables differ between versions and between customised databases — so it ends by *probing* the
#: refusal and reporting the observed answer rather than the expected one. That probe is what
#: produced the finding above.
DEFAULT_GROUPS: tuple[str, ...] = ("Role / Portal",)

#: The one name that maps the restricted fixture (runbook decision 2). ``MONI_ODOO_TEST_SUB_3`` is
#: retired: two names for one fixture is how a test silently runs as the wrong user.
DEFAULT_SUB_VAR = "MONI_ODOO_RESTRICTED_SUB"

#: The variable holding the fixture's own Odoo secret, in the API-key position.
DEFAULT_CREDENTIAL_VAR = "ODOO_TEST_VIEWER_KEY"

#: Environment variables the **operator** credential may arrive in, in precedence order. The first
#: two are what the restricted-fixture procedure uses — passed in-shell for single commands. The
#: rest are the older ``.env`` placeholders, kept so an operator who already filled them still works.
OPERATOR_LOGIN_VARS: tuple[str, ...] = ("ODOO_ADMIN_LOGIN", "ODOO_PROJECTREAD_PROVISION_LOGIN")
OPERATOR_KEY_VARS: tuple[str, ...] = (
    "ODOO_ADMIN_PASSWORD",
    "ODOO_ADMIN_KEY",
    "ODOO_PROJECTREAD_PROVISION_KEY",
)

#: Groups whose presence would prove the fixture is over-privileged. Reported loudly: a user who can
#: create tasks cannot demonstrate a refusal, and the whole point of the fixture is the refusal.
FORBIDDEN_GROUP_MARKERS: tuple[str, ...] = (
    "project / administrator",
    "administrator",
    "settings",
)


@dataclass(frozen=True, slots=True)
class Fixture:
    """One restricted fixture: who it is in each system, and how it starts out."""

    keycloak_username: str
    odoo_login: str
    display_name: str
    groups: tuple[str, ...]
    realm_role: str
    #: The ``.env`` variable the resolved Keycloak subject is written to.
    sub_var: str
    #: The variable holding this fixture's own Odoo secret.
    credential_var: str

    def describe(self) -> str:
        return f"{self.keycloak_username!r} -> {self.odoo_login!r} in {list(self.groups)}"


def read_env() -> dict[str, str]:
    """Parse the root ``.env`` (UTF-8, BOM tolerated) into a mapping."""
    values: dict[str, str] = {}
    if not ROOT_ENV.is_file():
        msg = f"{ROOT_ENV} not found; create it from .env.example first"
        raise SystemExit(msg)
    for raw in ROOT_ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _usable(value: str) -> bool:
    return bool(value) and "change-me" not in value


def resolve_secret(values: dict[str, str], name: str) -> str:
    """The named secret: the **process environment first**, then the root ``.env``.

    The order is the point rather than a convenience. The restricted-fixture procedure passes the
    operator credential as an in-shell environment variable for single commands and requires that it
    never reaches ``.env`` (runbook decision 3, ``docs/runbooks/restricted-fixture-proof.md``). A
    standing fixture credential, by contrast, is allowed to sit in ``.env`` like the other three DEV
    fixtures — which is why ``.env`` is consulted at all, and why this is not simply ``os.environ``.
    """
    from_environment = os.environ.get(name, "").strip()
    if _usable(from_environment):
        return from_environment
    from_env_file = values.get(name, "").strip()
    if _usable(from_env_file):
        return from_env_file
    msg = (
        f"{name} is not set: checked the process environment, then {ROOT_ENV.name}. "
        "For the operator credential, export it in the shell hosting this command (never .env)."
    )
    raise SystemExit(msg)


def resolve_operator(values: dict[str, str]) -> tuple[str, str, str, str]:
    """The operator login and secret, and the variable names they came from.

    Returns ``(login_var, login, key_var, key)``; the variable names are returned so the report can
    say *where* a credential was used without ever printing its value — which is what the runbook
    asks for when it says to report where they were used.
    """
    for login_var in OPERATOR_LOGIN_VARS:
        login = os.environ.get(login_var, "").strip() or values.get(login_var, "").strip()
        if not _usable(login):
            continue
        for key_var in OPERATOR_KEY_VARS:
            key = os.environ.get(key_var, "").strip() or values.get(key_var, "").strip()
            if _usable(key):
                return login_var, login, key_var, key
    return "", "", "", ""


def write_back_env(values: dict[str, str], updates: dict[str, str]) -> list[str]:
    """Set ``updates`` in the root ``.env``, in place, preserving comments and order.

    Rewriting the file here rather than asking the operator to hand-edit a 300-line UTF-8 file is
    the same call ``scripts/remap_odoo_users.py`` makes — and doing it in Python rather than
    PowerShell is deliberate: a read-modify-write of this file in PowerShell corrupts UTF-8.

    Only the **subject** is ever written this way. A *credential* is not: it stays in the shell that
    supplied it (runbook decision 3).
    """
    changed: list[str] = []
    lines: list[str] = []
    seen: set[str] = set()
    for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
        name, separator, current = line.partition("=")
        if separator and name in updates:
            seen.add(name)
            if current != updates[name]:
                changed.append(f"{name}: {current} -> {updates[name]}")
                lines.append(f"{name}={updates[name]}")
                continue
        lines.append(line)
    for name, value in updates.items():
        if name not in seen:
            lines.append(f"{name}={value}")
            changed.append(f"{name}: (new) -> {value}")
    if changed:
        ROOT_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return changed


@dataclass
class Report:
    """One line per step, printed at the end so a rerun is comparable with the first run."""

    lines: list[str]

    def did(self, subject: str, action: str, detail: str = "") -> None:
        suffix: str = f" — {detail}" if detail else ""
        self.lines.append(f"{action:<9} {subject}{suffix}")
        print(f"  {action:<9} {subject}{suffix}")

    def to_text(self) -> str:
        return "\n".join(self.lines) if self.lines else "(nothing to report)"


# ---------------------------------------------------------------------------
# Keycloak
# ---------------------------------------------------------------------------


class Keycloak:
    """The small slice of the Keycloak admin API this fixture needs, over httpx.

    Deliberately the same shape as ``scripts/remap_odoo_users.py``'s ``keycloak_subs`` — one
    admin token from the master realm, then the realm's own admin API — so there is one story about
    how this deployment talks to Keycloak rather than two.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._issuer = values.get("KEYCLOAK_ISSUER", "http://127.0.0.1:8081").rstrip("/")
        self._realm = values.get("KEYCLOAK_REALM", "moni")
        token = httpx.post(
            f"{self._issuer}/realms/master/protocol/openid-connect/token",
            data={
                "username": resolve_secret(values, "KEYCLOAK_ADMIN"),
                "password": resolve_secret(values, "KEYCLOAK_ADMIN_PASSWORD"),
                "grant_type": "password",
                "client_id": "admin-cli",
            },
            timeout=30.0,
        )
        token.raise_for_status()
        self._headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

    @property
    def realm(self) -> str:
        return self._realm

    def find_user(self, username: str) -> dict[str, object] | None:
        response = httpx.get(
            f"{self._issuer}/admin/realms/{self._realm}/users",
            headers=self._headers,
            params={"username": username, "exact": "true"},
            timeout=30.0,
        )
        response.raise_for_status()
        found = response.json()
        return found[0] if found else None

    def realm_roles(self, user_id: str) -> list[str]:
        response = httpx.get(
            f"{self._issuer}/admin/realms/{self._realm}/users/{user_id}/role-mappings/realm",
            headers=self._headers,
            timeout=30.0,
        )
        response.raise_for_status()
        return sorted(str(role["name"]) for role in response.json())

    def role_representation(self, name: str) -> dict[str, object]:
        response = httpx.get(
            f"{self._issuer}/admin/realms/{self._realm}/roles/{name}",
            headers=self._headers,
            timeout=30.0,
        )
        response.raise_for_status()
        return dict(response.json())

    def create_user(self, username: str, password: str, display_name: str) -> str:
        """Create the user and return its new ``sub``. Idempotency is the caller's job."""
        first, _, last = display_name.partition(" ")
        response = httpx.post(
            f"{self._issuer}/admin/realms/{self._realm}/users",
            headers=self._headers,
            json={
                "username": username,
                "enabled": True,
                "emailVerified": True,
                "firstName": first or username,
                "lastName": last or "Fixture",
                "email": f"{username}@moni.local",
                "attributes": {
                    "moniRoleDescription": [
                        "Restricted DEV fixture: writes must be refused by Odoo's own ACL"
                    ]
                },
                "credentials": [{"type": "password", "value": password, "temporary": False}],
                "requiredActions": [],
            },
            timeout=30.0,
        )
        if response.status_code not in (201, 204):
            msg = f"Keycloak refused to create {username}: http {response.status_code}"
            raise SystemExit(msg)
        created = self.find_user(username)
        if created is None:  # pragma: no cover - 201 means it exists
            msg = f"Keycloak reported {username} created but it cannot be read back"
            raise SystemExit(msg)
        return str(created["id"])

    def ensure_role(self, user_id: str, role: str) -> bool:
        """Grant ``role`` if absent. Returns whether anything changed."""
        if role in self.realm_roles(user_id):
            return False
        response = httpx.post(
            f"{self._issuer}/admin/realms/{self._realm}/users/{user_id}/role-mappings/realm",
            headers=self._headers,
            json=[self.role_representation(role)],
            timeout=30.0,
        )
        if response.status_code not in (201, 204):
            msg = f"Keycloak refused to grant {role!r}: http {response.status_code}"
            raise SystemExit(msg)
        return True


# ---------------------------------------------------------------------------
# Odoo
# ---------------------------------------------------------------------------


class Odoo:
    """The Odoo half, over the same typed client the tools use — but through the fixture hatch.

    ``OdooClient.execute_kw`` refuses every mutation outside the tool allowlist (correctly: that is
    §3.3). Assigning a group is outside it, so this uses ``fixture_execute_kw`` — the named hatch
    that refuses unless ``MONI_ENV=dev`` and never deletes — exactly as ``scripts/seed_s22714.py``
    does for its MRP fixture. The alternative would be widening the tool allowlist, which is the one
    thing the write task forbids.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def find_user(self, login: str) -> list[dict[str, object]]:
        rows = await self._client.fixture_execute_kw(  # type: ignore[attr-defined]
            "res.users",
            "search_read",
            [[["login", "=", login]], ["id", "login", "name", "active", "group_ids"]],
        )
        return [dict(row) for row in rows]

    async def group_names(self) -> dict[int, str]:
        # `fixture_execute_kw`'s third argument is `execute_kw`'s *positional* argument list, so for
        # `search_read` that is `[domain, fields]` — the domain itself, not a list wrapping one.
        # This read `[[[]], …]`, which is a domain containing a single *empty condition*, and Odoo 19
        # rejects it with `Domain() invalid item in domain: []`. It survived review because nothing
        # executed this script until an operator credential existed to run it with: the same
        # "proved only by a scripted transport" failure ADR 0009 records for the write path, here in
        # the fixture tooling rather than the tools.
        rows = await self._client.fixture_execute_kw(  # type: ignore[attr-defined]
            "res.groups", "search_read", [[], ["id", "name", "full_name"]]
        )
        return {int(row["id"]): str(row.get("full_name") or row.get("name") or "") for row in rows}

    async def group_ids_for(self, names: tuple[str, ...], known: dict[int, str]) -> list[int]:
        wanted: list[int] = []
        for name in names:
            matches = [gid for gid, label in known.items() if label == name]
            if len(matches) != 1:
                msg = (
                    f"Odoo group {name!r} matched {len(matches)} groups on this stand "
                    f"({[known.get(gid) for gid in matches]}); refusing to guess"
                )
                raise SystemExit(msg)
            wanted.append(matches[0])
        return wanted

    async def create_user(self, login: str, password: str, name: str) -> int:
        values: dict[str, object] = {
            "login": login,
            "name": name,
            "password": password,
            "group_ids": [(6, 0, [])],
        }
        created = await self._client.fixture_execute_kw(  # type: ignore[attr-defined]
            "res.users", "create", [[values]]
        )
        # Odoo 19 answers `create` with a **list** of ids when it was handed a list, and a bare id
        # when it was handed one dict. Both shapes are accepted, and the shape this stand actually
        # produces is named rather than assumed: `[[values]]` (a one-element list) returns `[id]`.
        # This is the sibling of the defect ADR 0009 records — `message_post` returns a one-element
        # list, the code required an `int`, every unit test agreed with the author, and every real
        # chatter write failed. It was live here until this script first had an operator credential
        # to run with.
        new_id = created[0] if isinstance(created, (list, tuple)) else created
        return int(new_id)

    async def set_groups(self, uid: int, group_ids: list[int]) -> None:
        await self._client.fixture_execute_kw(  # type: ignore[attr-defined]
            "res.users", "write", [[uid], {"group_ids": [(6, 0, group_ids)]}]
        )

    async def can_create_task(self) -> tuple[bool, str]:
        """Attempt a real ``project.task`` create as the *fixture* user.

        Returns ``(refused, message)``. This is the empirical step the task demands: the refusal is
        *verified*, never assumed from a group name. A `create` that succeeds is reported honestly —
        the fixture then needs a different group set, and saying so is more useful than a test that
        silently proves nothing.
        """
        try:
            created = await self._client.fixture_execute_kw(  # type: ignore[attr-defined]
                "project.task",
                "create",
                [[{"name": "MONI provision probe — leave in place"}]],
            )
        except Exception as exc:  # noqa: BLE001 - any typed refusal is the answer we want
            return True, f"{type(exc).__name__}: {exc}"
        return False, f"create SUCCEEDED (id={created}) — this group set does not refuse"


async def provision_odoo(
    values: dict[str, str], *, spec: Fixture, dry_run: bool, report: Report
) -> int:
    """Provision the Odoo half. Returns a process exit code."""
    from moni_mcp_odoo.client import OdooClient
    from moni_mcp_odoo.credentials import OdooSettings

    login_var, operator_login, key_var, operator_key = resolve_operator(values)
    if not operator_login:
        report.did(
            "odoo",
            "BLOCKED",
            f"no operator credential in {'/'.join(OPERATOR_LOGIN_VARS)} — and no other account on "
            "this stand may write res.users/res.groups. Export it in the shell hosting this command "
            "(never .env); see docs/runbooks/restricted-fixture-proof.md.",
        )
        return 2

    password = resolve_secret(values, spec.credential_var)
    settings = OdooSettings.from_env()
    client = OdooClient(
        base_url=settings.url,
        database=settings.database,
        login=operator_login,
        api_key=operator_key,
        timeout_seconds=settings.timeout_seconds,
    )
    odoo = Odoo(client)
    try:
        await client.authenticate()
        # Which variable supplied the credential, never the credential itself (§3.11).
        report.did("operator", "authenticated", f"as {operator_login} via ${login_var}/${key_var}")

        known = await odoo.group_names()
        wanted_ids = await odoo.group_ids_for(spec.groups, known)
        for gid in wanted_ids:
            label = known.get(gid, str(gid))
            if any(marker in label.lower() for marker in FORBIDDEN_GROUP_MARKERS):
                # Asking for an over-privileged group is a mistake in the arguments, not a property
                # of the stand: a fixture carrying it cannot demonstrate the refusal it exists for,
                # so this stops rather than provisioning something that proves nothing.
                report.did(f"res.groups {gid}", "REFUSED", f"{label!r} is over-privileged")
                report.did(
                    "fixture",
                    "UNSUITABLE",
                    "an over-privileged group was requested; choose a narrower --groups",
                )
                return 3
            report.did(f"res.groups {gid}", "wanted", label)

        existing = await odoo.find_user(spec.odoo_login)
        if existing:
            uid = int(str(existing[0]["id"]))
            report.did(
                f"res.users {uid}", "exists", f"{spec.odoo_login} active={existing[0]['active']}"
            )
            if not dry_run:
                await odoo.set_groups(uid, wanted_ids)
                report.did(f"res.users {uid}", "groups set", str(spec.groups))
        else:
            if dry_run:
                report.did("res.users", "would create", spec.odoo_login)
                return 0
            uid = await odoo.create_user(spec.odoo_login, password, spec.display_name)
            report.did(f"res.users {uid}", "created", spec.odoo_login)
            await odoo.set_groups(uid, wanted_ids)
            report.did(f"res.users {uid}", "groups set", str(spec.groups))

        if dry_run:
            return 0

        resolved = await odoo.find_user(spec.odoo_login)
        raw_groups = resolved[0].get("group_ids")
        current_group_ids = (
            [int(str(gid)) for gid in raw_groups] if isinstance(raw_groups, list) else []
        )
        names = [known.get(gid, str(gid)) for gid in current_group_ids]
        report.did(f"res.users {uid}", "groups are", ", ".join(sorted(names)))

        # The empirical refusal probe, as the fixture user.
        probe_client = OdooClient(
            base_url=settings.url,
            database=settings.database,
            login=spec.odoo_login,
            api_key=password,
            timeout_seconds=settings.timeout_seconds,
        )
        try:
            await probe_client.authenticate()
            refused, message = await Odoo(probe_client).can_create_task()
        finally:
            await probe_client.aclose()
        if refused:
            report.did("project.task.create", "REFUSED", message)
            return 0
        report.did("project.task.create", "ALLOWED", message)
        report.did(
            "fixture",
            "UNSUITABLE",
            "this group set can create tasks; pass a narrower --groups and re-run",
        )
        return 3
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    values = read_env()
    spec = fixture_from_args(args)
    os.environ.update({k: v for k, v in values.items() if k not in os.environ})
    os.environ.setdefault("MONI_ENV", values.get("MONI_ENV", "dev"))
    report = Report([])

    print(f"environment: {ROOT_ENV}")
    print(f"fixture    : keycloak {spec.keycloak_username!r} (role {spec.realm_role})")
    print(f"             odoo {spec.odoo_login!r} starting in {list(spec.groups)}")
    print(f"subject to : {spec.sub_var} (in {ROOT_ENV.name})")

    # --- Keycloak -----------------------------------------------------------
    keycloak = Keycloak(values)
    test_password = resolve_secret(values, "MONI_TEST_USER_PASSWORD")
    existing = keycloak.find_user(spec.keycloak_username)
    if existing is None:
        if args.dry_run:
            report.did(
                f"keycloak {spec.keycloak_username}", "would create", f"role={spec.realm_role}"
            )
            sub = ""
        else:
            sub = keycloak.create_user(spec.keycloak_username, test_password, spec.display_name)
            report.did(f"keycloak {spec.keycloak_username}", "created", f"sub={sub}")
    else:
        sub = str(existing["id"])
        report.did(f"keycloak {spec.keycloak_username}", "exists", f"sub={sub}")

    if not sub:
        print("-" * 60)
        print(report.to_text())
        return 0

    if not args.dry_run:
        if keycloak.ensure_role(sub, spec.realm_role):
            report.did(f"keycloak {sub}", "role granted", spec.realm_role)
        else:
            report.did(f"keycloak {sub}", "role present", spec.realm_role)
        report.did(f"keycloak {sub}", "realm roles", ", ".join(keycloak.realm_roles(sub)))

    # --- .env wiring: the subject only, never a credential ------------------
    if not args.dry_run:
        changed = write_back_env(values, {spec.sub_var: sub})
        for change in changed:
            report.did(".env", "updated", change)

    # --- Odoo ---------------------------------------------------------------
    code = await provision_odoo(values, spec=spec, dry_run=args.dry_run, report=report)

    print("-" * 60)
    if code == 2:
        print("NEXT: this stand has no operator credential that may create users or assign groups.")
        print("      Export it for THIS command only and re-run, e.g.:")
        print("        $env:ODOO_ADMIN_LOGIN='...'; $env:ODOO_ADMIN_PASSWORD='...'")
        print("      Never a shared admin, never in .env.")
    elif code == 3:
        print("NEXT: the probe says this group set can create tasks, so it cannot prove a refusal.")
        print("      Pass a narrower --groups and re-run.")
    elif code == 0 and not args.dry_run:
        print(
            f"NEXT: ensure {spec.credential_var} is in {ROOT_ENV.name} (the script does NOT write"
        )
        print("      credentials there), then:")
        print("        uv run --group dev python scripts/remap_odoo_users.py")
        print(f"      then the live suite with {spec.sub_var} mapped.")
    return code


def fixture_from_args(args: argparse.Namespace) -> Fixture:
    """The fixture this invocation provisions, from the command line."""
    name = args.fixture.strip()
    if not name:
        raise SystemExit("--fixture must not be empty")
    groups = tuple(part.strip() for part in args.groups.split(",") if part.strip())
    return Fixture(
        keycloak_username=args.keycloak_username or name,
        odoo_login=args.odoo_login or f"{name}@moni.test",
        display_name=args.display_name or name.replace("_", " ").replace(".", " ").title(),
        groups=groups or DEFAULT_GROUPS,
        realm_role=args.realm_role,
        sub_var=args.sub_var,
        credential_var=args.credential_var,
    )


def build_parser() -> argparse.ArgumentParser:
    """The command line, in one place so a test can assert the contract without restating it."""
    parser = argparse.ArgumentParser(prog="provision_projectread_user", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--fixture",
        default="viewer",
        help="fixture name; derives the Keycloak username and <name>@moni.test (default: viewer)",
    )
    parser.add_argument("--keycloak-username", default="", help="override the Keycloak username")
    parser.add_argument("--odoo-login", default="", help="override the Odoo login")
    parser.add_argument("--display-name", default="", help="res.users display name")
    parser.add_argument(
        "--groups",
        default=",".join(DEFAULT_GROUPS),
        help=(
            "comma-separated Odoo group *full names* the fixture starts in. The default is the "
            "restricted set — Role / Portal, which Odoo genuinely refuses project.task.create for "
            "on Odoo 19 (an Internal User is *allowed*, because the To-do app grants it). Pass "
            '"Internal User,Project / User" for the read-only project fixture.'
        ),
    )
    parser.add_argument("--realm-role", default=DEFAULT_REALM_ROLE, help="Keycloak realm role")
    parser.add_argument(
        "--sub-var",
        default=DEFAULT_SUB_VAR,
        help=f"the .env variable the resolved subject is written to (default: {DEFAULT_SUB_VAR})",
    )
    parser.add_argument(
        "--credential-var",
        default=DEFAULT_CREDENTIAL_VAR,
        help=(
            "the variable holding this fixture's own Odoo secret; the process environment is "
            f"checked first, then .env (default: {DEFAULT_CREDENTIAL_VAR})"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
