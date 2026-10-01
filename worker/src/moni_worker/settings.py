"""The worker's queue discipline: concurrency, timeouts and the poll interval (task 2.6).

Every number here is a decision rather than a default, and each is derived from something that already
exists instead of being restated:

* **`max_jobs` 2.** Small on purpose. The worker's runs call the same local model the interactive path
  calls, so a large global concurrency would let background work starve the chat that a person is
  waiting on. Two is enough to keep the queue moving while a run waits on I/O.
* **Per-user concurrency 1.** A second trigger for one user queues (see `gate.py`), because a person's
  runs share a mailbox, a budget and a reviewer.
* **`job_timeout` = the run's wall-clock budget + a margin**, read from the *same* `RunLimits` the agent
  enforces. Two numbers that must agree are two numbers that will drift, so this one is computed: a job
  timeout below the run budget would kill runs the agent considers legal, and one far above it would
  hold a worker slot for a run that has already given up.
* **`POLL_MINUTES` 5.** The trigger's freshness: how long a new email waits before the agent sees it.

`build_worker_settings` returns a *class* because that is arq's interface (`arq WorkerSettings` is read
as attributes), and building it from a config object is what lets the numbers be asserted in a test
rather than grepped out of a module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from moni_agent.limits import RunLimits

#: How much longer than the run's own budget a job may live. The margin covers the work around the run
#: — building the toolbox, the MCP handshake, writing the audit row — which is real time the run's
#: budget does not count.
DEFAULT_TIMEOUT_MARGIN_SECONDS: Final = 30.0

DEFAULT_MAX_JOBS: Final = 2
DEFAULT_PER_USER_CONCURRENCY: Final = 1
DEFAULT_POLL_MINUTES: Final = 5

#: How long a finished job's result is kept in Redis. An hour is enough for an operator to ask what
#: happened, and short enough that the queue's memory does not grow without bound.
DEFAULT_KEEP_RESULT_SECONDS: Final = 3600

#: The values that mean "nobody has filled this in yet". A mailbox whose credentials are still the
#: template's placeholders is *not* configured and is treated exactly like a missing one — the
#: alternative is a worker that starts polling and spends every cycle failing to authenticate.
ZOHO_CREDENTIAL_VARS: Final = (
    "ZOHO_DC",
    "ZOHO_ACCOUNT_ID",
    "ZOHO_CLIENT_ID",
    "ZOHO_CLIENT_SECRET",
    "ZOHO_REFRESH_TOKEN",
)
PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset({"", "change-me"})

#: The tools a triggered run may never use, whatever roles it is configured with.
#:
#: **Why this is an explicit subtraction rather than a role.** A trigger's tool scope is a property of
#: *the trigger*, not of the person it acts for: it exists to read a message and prepare a draft, and
#: the send is a separate act a human approves. No current role expresses that — `manager` grants the
#: mail reads *and* `send_message` — so relying on roles alone would give a background run the power
#: to send mail unattended, which is the one thing §3.3's stricter class exists to prevent.
#:
#: The consequence worth stating: a triggered run's tool set is **strictly narrower** than the same
#: roles' interactive set, by construction. `tests/unit/worker/test_trigger_scope.py` asserts the
#: strictness, and its anti-vacuity twin fails if this set is ever emptied — because then the config
#: would constrain nothing and the assertion would be about nothing.
TRIGGER_WITHHELD_TOOLS: Final[frozenset[str]] = frozenset({"send_message"})


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """The worker's knobs, resolved once at startup."""

    redis_url: str
    max_jobs: int
    per_user_concurrency: int
    job_timeout_seconds: float
    poll_minutes: int
    poll_folder: str
    #: Whether the polling trigger may run. **False does not stop the worker**: it starts, passes its
    #: healthcheck and serves whatever else it is asked for — only the poll is dormant, and it makes
    #: no attempt to reach Zoho. §3.12's degrading direction is "serve less", not "refuse to run": a
    #: worker that exited without a mailbox could not be used for anything it *is* able to do.
    polling_enabled: bool
    #: The Keycloak subject a triggered run acts as: the mailbox owner (§3.2 — there is no system
    #: account, and somebody has to see the approval card and decide).
    trigger_user_sub: str
    #: The roles that run carries, **declared here rather than looked up in Keycloak**.
    #:
    #: A lookup would hand the trigger the owner's *current* roles, so a change made for an unrelated
    #: reason would silently widen or narrow what background runs can do. This is a capability of the
    #: trigger, and it belongs where it can be read and audited.
    #:
    #: The cost is accepted and recorded in ADR 0013: after the owner is offboarded in Keycloak the
    #: trigger keeps working with these declared roles. Acceptable while there is one trigger; to be
    #: revisited when there are more.
    trigger_roles: tuple[str, ...]
    #: The Zoho account the mailbox belongs to, recorded on every ledger row so "which mailbox did
    #: this come from" is answerable without the environment it ran in.
    zoho_account_id: str
    keep_result_seconds: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerConfig:
        """Read the worker's own variables, and the agent's budgets for the timeout.

        `run_limits` is read here rather than passed in so that the timeout is computed from the same
        environment the agent will use at run time — the two cannot disagree about the budget.
        """
        source = os.environ if env is None else env
        limits = RunLimits.from_env(dict(source))
        return cls(
            redis_url=_str(source, "MONI_REDIS_URL", "redis://redis:6379/0"),
            max_jobs=_int(source, "WORKER_MAX_JOBS", DEFAULT_MAX_JOBS),
            per_user_concurrency=_int(
                source, "WORKER_PER_USER_CONCURRENCY", DEFAULT_PER_USER_CONCURRENCY
            ),
            job_timeout_seconds=limits.wall_clock_seconds
            + _float(source, "WORKER_TIMEOUT_MARGIN_SECONDS", DEFAULT_TIMEOUT_MARGIN_SECONDS),
            poll_minutes=_int(source, "POLL_MINUTES", DEFAULT_POLL_MINUTES),
            poll_folder=_str(source, "POLL_FOLDER", "INBOX"),
            polling_enabled=_mailbox_configured(source),
            trigger_user_sub=_str(source, "TRIGGER_USER_SUB", ""),
            trigger_roles=_roles(source),
            zoho_account_id=_str(source, "ZOHO_ACCOUNT_ID", ""),
            keep_result_seconds=_int(
                source, "WORKER_KEEP_RESULT_SECONDS", DEFAULT_KEEP_RESULT_SECONDS
            ),
        )

    @property
    def lock_ttl_seconds(self) -> int:
        """The per-user lock's lifetime.

        Slightly longer than the job timeout, so the lock cannot expire while a run it protects is
        still alive — which would let a second run start in parallel, the exact failure it prevents.
        """
        return int(self.job_timeout_seconds) + 5


def build_worker_settings(
    config: WorkerConfig,
    *,
    functions: Sequence[Any] = (),
    cron_jobs: Sequence[Any] = (),
    on_startup: Any = None,
    on_shutdown: Any = None,
) -> type[Any]:
    """Build the class arq's CLI expects, from resolved configuration.

    A class rather than an instance because arq reads `WorkerSettings` as attributes off whatever the
    entry point names. Building it here means the numbers have exactly one home and a test can assert
    them without a Redis connection.

    **The lifecycle hooks have to be passed through, and that is not decoration.** arq's CLI builds the
    worker with ``get_kwargs(settings)``, which keeps only the names present in the settings class's
    own ``__dict__`` *and* in ``Worker.__init__``'s signature. A hook that is not set as an attribute
    here is therefore never called — and a `main.on_startup` that never runs is worse than one that
    fails, because the worker starts healthily with an empty `ctx` and every job then raises
    `KeyError`. That is exactly what happened to the polling trigger: `on_startup` existed, was
    documented, was reviewed — and was dead code until F3 registered it here.
    """
    from arq.connections import RedisSettings

    # Bound to different names before the class body: a class scope cannot read an enclosing local
    # that it also assigns, so `functions = list(functions)` raises NameError inside `class`.
    registered = list(functions)
    scheduled = list(cron_jobs)
    startup = on_startup
    shutdown = on_shutdown

    class WorkerSettings:
        redis_settings = RedisSettings.from_dsn(config.redis_url)
        functions = registered
        cron_jobs = scheduled
        # Both are read by arq only if the *class* defines them (see the docstring): `None` here would
        # be the same as omitting them, so the hooks themselves own what "no hook" means.
        on_startup = startup
        on_shutdown = shutdown
        max_jobs = config.max_jobs
        job_timeout = config.job_timeout_seconds
        keep_result = config.keep_result_seconds
        # A run that is waiting for an approval is *finished* as far as the queue is concerned — the
        # pause is persisted in the checkpoint, not held in a worker slot. So jobs are never aborted
        # for being "stuck" on a human, which is what this flag would otherwise do.
        allow_abort_jobs = True
        # No default retry on failure: a retry is a second run, and §3.7's reasoning applies — the
        # message ledger decides whether a re-drive is allowed, not the queue's error handling.
        max_tries = 1

    return WorkerSettings


def _roles(env: Mapping[str, str]) -> tuple[str, ...]:
    """The trigger's declared roles, checked against the vocabulary the platform knows.

    Fail closed on an unknown role (§3.12): `TRIGGER_ROLES=managr` must stop the worker rather than
    grant nothing silently — a background run whose tools quietly disappeared would look like a model
    that had stopped using them, and the difference would be nearly impossible to see.

    Emptiness is allowed and means "no roles", which grants nothing. That is the honest state for a
    deployment that has not configured the trigger yet, and it is not the same as a typo.
    """
    from moni_gateway.rbac import KNOWN_ROLES

    raw = (env.get("TRIGGER_ROLES") or "").strip()
    roles = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    unknown = sorted(set(roles) - KNOWN_ROLES)
    if unknown:
        msg = (
            f"TRIGGER_ROLES names {unknown}, which the platform does not know; "
            f"known roles: {sorted(KNOWN_ROLES)}"
        )
        raise ValueError(msg)
    return roles


def _mailbox_configured(env: Mapping[str, str]) -> bool:
    """Whether the polling trigger has a mailbox to poll.

    Every value must be present **and** not still a template placeholder. A `change-me` credential is
    the same situation as a missing one — the trigger cannot run — and treating it as configured would
    produce a worker that polls on a schedule and fails to authenticate every time, which reads in the
    logs as a Zoho problem rather than as an unconfigured deployment.
    """
    return all(
        (env.get(name) or "").strip() not in PLACEHOLDER_VALUES for name in ZOHO_CREDENTIAL_VARS
    )


def _str(env: Mapping[str, str], name: str, default: str) -> str:
    value = (env.get(name) or "").strip()
    return value or default


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    return int(raw) if raw else default


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    return float(raw) if raw else default


__all__ = [
    "DEFAULT_MAX_JOBS",
    "DEFAULT_PER_USER_CONCURRENCY",
    "DEFAULT_POLL_MINUTES",
    "DEFAULT_TIMEOUT_MARGIN_SECONDS",
    "WorkerConfig",
    "build_worker_settings",
]
