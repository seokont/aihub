#!/usr/bin/env python3
"""Phase 0 environment audit (CLAUDE.md §3.1, §3.11, §6).

Four checks, each of which has failed in a real project before:

1. **No service exposed beyond loopback** — every published port in the dev compose
   file must be bound to ``127.0.0.1``, and the application services (gateway and the
   databases) must publish nothing at all.
2. **``.env.example`` is complete** — every settings alias declared by the gateway, and
   every ``${VAR}`` referenced by the compose files, must be documented there.
3. **No secret is committed** — no tracked ``.env`` (any variant other than
   ``.env.example``), no credential-looking literal in the compose files, the Alembic
   config or the tracked Python sources.
4. **No transcoding damage** — every text file must be valid UTF-8 and free of CP1251
   mojibake or question-mark substitution damage. This has reached the tree twice: once
   through a clipboard interceptor, once through a PowerShell round-trip that re-encoded
   UTF-8 as CP1251. Both times the code still ran, so only a check like this finds it.

Usage::

    python scripts/check_environment.py            # human-readable report
    python scripts/check_environment.py --json     # machine-readable

Exit code is 0 when every check passes, 1 otherwise, so CI and ``make check-env`` can
depend on it. Standard library only: this must run before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_COMPOSE = REPO_ROOT / "infra" / "docker-compose.dev.yml"
SERVER_COMPOSE = REPO_ROOT / "infra" / "docker-compose.server.yml"
#: The file whose published ports are the *dev host's* business. The loopback rule checked below
#: exists to keep a developer laptop's stack off the LAN; on the server, ingress is deliberately a
#: different question (CLAUDE.md §4 wants 80/443 public behind TLS), so that overlay is not
#: subjected to it.
COMPOSE_FILES = (DEV_COMPOSE,)
#: Every compose file this stack can be run with. A variable referenced in any of them must be
#: documented, and none of them may carry a credential literal.
ALL_COMPOSE_FILES = (DEV_COMPOSE, SERVER_COMPOSE)
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Files whose inline configuration must not contain credentials.
CONFIG_FILES = (
    REPO_ROOT / "db" / "alembic.ini",
    *ALL_COMPOSE_FILES,
)

# A value assigned in a config file that still looks like a credential.
SECRET_LITERAL = re.compile(
    r"""(?ix)
    (password|passwd|secret|token|api[_-]?key|private[_-]?key)  # a credential name
    \s*[:=]\s*                                                  # an assignment
    ["']?                                                       # optional quote
    (?P<value>[A-Za-z0-9_@!#$%^&*+./-]{8,})                     # an actual value
    """
)

# Values that are obviously not secrets, even when they sit next to a credential name.
ALLOWED_VALUE_HINTS = (
    "${",
    "$",
    "change-me",
    "changeme",
    "postgresql",
    "postgres:",
    "127.0.0.1",
    "0.0.0.0",
    "required",
    "must be",
    "see .env",
    "env var",
    "path",
    "docker",
    "internal",
)


@dataclass
class Report:
    """Collected findings for one audit run."""

    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def _published_ports(compose_text: str) -> list[str]:
    """Extract host port mappings from a compose document.

    Implemented as a small parser rather than one regex, because the host side is
    usually a variable (``- "127.0.0.1:${NGINX_HTTP_PORT:-80}:80"``) and the mapping
    may be written as ``HOST:CONTAINER`` or ``IP:HOST:CONTAINER``. Anything that is
    not a quoted mapping under a ``ports:`` key is ignored.
    """
    published: list[str] = []
    in_ports = False
    ports_indent = 0
    for line in compose_text.splitlines():
        stripped = line.strip()
        if stripped == "ports:":
            in_ports = True
            ports_indent = len(line) - len(line.lstrip())
            continue
        if not in_ports:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= ports_indent:
            in_ports = False
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("\"'")
            if value:
                published.append(value)
    return published


def _host_binding(mapping: str) -> str:
    """The host side of a port mapping, with the container port removed.

    ``127.0.0.1:${KEYCLOAK_PORT:-8081}:8080`` -> ``127.0.0.1``
    ``${NGINX_HTTP_PORT:-80}:80``             -> ``0.0.0.0`` (no host given)
    """
    body = mapping.split("/", 1)[0]
    parts = body.rsplit(":", 1)  # drop the container port
    if not parts:
        return ""
    remainder = parts[0]
    if remainder.count(":") >= 1 and not remainder.startswith("${"):
        return remainder.split(":", 1)[0]
    return "0.0.0.0"  # no host address: docker binds every interface


def check_ports(report: Report) -> None:
    """Every published port must be loopback-bound; app services must publish none."""
    published: list[tuple[str, str]] = []
    for compose_file in COMPOSE_FILES:
        if not compose_file.is_file():
            report.fail(f"missing compose file: {compose_file.name}")
            continue
        text = compose_file.read_text(encoding="utf-8")
        published.extend((compose_file.name, mapping) for mapping in _published_ports(text))
        if re.search(r"^\s*-\s*\"?0\.0\.0\.0:", text, re.MULTILINE):
            report.fail(f"{compose_file.name}: a port is bound to 0.0.0.0")

    if not published:
        report.fail("no published ports were found — the checker's parser is stale")

    for source, mapping in published:
        host = _host_binding(mapping)
        if host != "127.0.0.1":
            report.fail(f"{source}: published port is not loopback-only: {mapping} (host={host})")

    report.checks["published_ports"] = [mapping for _, mapping in published]

    # Services that must never be reachable from the host at all.
    internal_services = ("gateway", "redis", "langfuse-db", "migrate")
    # postgres is deliberately PUBLISHED on the loopback interface: host-run tooling (the
    # gateway CLI, pytest resolving Odoo credentials) needs it. Loopback-only is still
    # required, and that is asserted for every published mapping above.
    loopback_published = ("postgres",)

    compose = COMPOSE_FILES[0].read_text(encoding="utf-8")
    for service in internal_services:
        block = _service_block(compose, service)
        if block is None:
            report.fail(f"compose has no service block for {service}")
            continue
        if "ports:" in block:
            report.fail(f"service {service} publishes a port; it must stay internal (§3.1)")

    for service in loopback_published:
        block = _service_block(compose, service)
        if block is None:
            report.fail(f"compose has no service block for {service}")
            continue
        if "ports:" not in block:
            report.fail(
                f"service {service} must publish its port on 127.0.0.1 so host-run "
                "tooling can reach it (see README.md)"
            )

    gateway_block = _service_block(compose, "gateway") or ""
    if "condition: service_completed_successfully" not in gateway_block:
        report.fail("the gateway does not wait for the migrate service to finish")
    report.checks["internal_services"] = list(internal_services)
    report.checks["loopback_published"] = list(loopback_published)


def _service_block(compose: str, service: str) -> str | None:
    """Return the YAML block of one service (two-space indented keys)."""
    match = re.search(
        rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", compose, re.MULTILINE | re.DOTALL
    )
    return match.group(0) if match else None


def check_env_example(report: Report) -> None:
    """Every referenced variable must be documented in .env.example."""
    if not ENV_EXAMPLE.is_file():
        report.fail(".env.example is missing")
        return

    documented = _env_example_keys()
    report.checks["documented_variables"] = sorted(documented)

    referenced: set[str] = set()
    for compose_file in ALL_COMPOSE_FILES:
        if not compose_file.is_file():
            report.fail(f"missing compose file: {compose_file.name}")
            continue
        text = compose_file.read_text(encoding="utf-8")
        # Comments are stripped first: prose explaining a pattern (for example a note that
        # Keycloak resolves `${VAR}` against its own environment) is not a reference, and
        # treating it as one produces a failure naming a variable that does not exist.
        code = "\n".join(
            line.split("#", 1)[0] for line in text.splitlines() if not line.strip().startswith("#")
        )
        referenced.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", code))

    try:
        sys.path.insert(0, str(REPO_ROOT / "gateway" / "src"))
        from moni_gateway.config import Settings

        settings_aliases = {
            field_info.alias for field_info in Settings.model_fields.values() if field_info.alias
        }
    except Exception as exc:  # noqa: BLE001 - reported as a note, not a failure
        settings_aliases = set()
        report.note(
            f"could not import the gateway settings ({type(exc).__name__}); "
            "checked compose references only"
        )

    report.checks["settings_aliases"] = sorted(settings_aliases)

    # Variables consumed by the app or compose but absent from the template. Compose
    # built-ins are excluded: they are provided by docker, not by the operator.
    compose_builtins = {"COMPOSE_PROJECT_NAME"}
    missing = (referenced | settings_aliases) - documented - compose_builtins
    for name in sorted(missing):
        report.fail(f"{name} is referenced but not documented in .env.example")


def check_secrets(report: Report) -> None:
    """No .env variant is tracked, and no config file carries a credential literal."""
    #: Env files that are expected and documented, so their presence is not a finding:
    #: the root `.env` (every developer is told to create it) and `ui/.env`, which is the
    #: UI container's own env_file, generated by scripts/gen_ui_secrets.py. Both are
    #: git-ignored. Everything else in our tree is a stray — that is how `infra/.env`
    #: leaked twice before.
    expected_env_files = {REPO_ROOT / ".env", REPO_ROOT / "ui" / ".env"}
    #: The LibreChat fork is a pinned third-party checkout. Its own test fixtures and
    #: example env files are upstream's business, and reporting them would make this audit
    #: noisy about code we do not own (it would also never be actionable).
    ignored_parts = {".git", "node_modules", ".uv-cache", ".venv", "ui"}

    for candidate in REPO_ROOT.rglob(".env*"):
        if candidate.name == ".env.example":
            continue
        if any(part in ignored_parts for part in candidate.parts):
            continue
        if candidate.parent == REPO_ROOT or candidate in expected_env_files:
            report.notes.append(
                f"{candidate.relative_to(REPO_ROOT)} present (expected for local development)"
            )
            continue
        report.fail(f"a stray env file is present in the tree: {candidate.relative_to(REPO_ROOT)}")

    for path in CONFIG_FILES:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = SECRET_LITERAL.search(stripped)
            if not match:
                continue
            value = match.group("value")
            if any(
                hint in value.lower() or hint in stripped.lower() for hint in ALLOWED_VALUE_HINTS
            ):
                continue
            report.fail(
                f"{path.relative_to(REPO_ROOT)}:{number} looks like a literal credential: {stripped}"
            )

    # The tracked Python sources must not embed a credential either.
    for path in (REPO_ROOT / "gateway" / "src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "password=" in line and "asyncpg://" in line and "change-me" not in line:
                report.fail(f"{path.relative_to(REPO_ROOT)}:{number} embeds a database password")
    report.checks["config_files_scanned"] = [str(p.relative_to(REPO_ROOT)) for p in CONFIG_FILES]


# --- 4. transcoding damage ---------------------------------------------------

#: Extensions whose contents are text we own. `.example` is what covers `.env.example`; the rest
#: is the documentation, code and configuration that has been damaged twice.
TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".py",
        ".yaml",
        ".yml",
        ".example",
        ".toml",
        ".txt",
        ".ps1",
        ".json",
        ".ini",
        ".conf",
        ".template",
        ".cfg",
    }
)

#: Not ours, or not text we author: the pinned LibreChat checkout, virtualenvs, caches.
ENCODING_SKIP_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        ".uv-cache",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "ui",
    }
)

#: A run of `?` where a character was *lost*: `?` is what a lossy transcode substitutes. The two
#: real occurrences were a section sign and an em dash, each replaced by a pair of question marks.
#: Two in a row is already impossible in prose and rare in code — measured across the whole tree it
#: matches exactly one line, and that line is legitimate (a shell parameter expansion stripping
#: characters), which is why expansions are removed before matching. This comment deliberately
#: spells none of those examples literally: writing them out here is what the check flags first.
QUESTION_RUN = re.compile(r"\?{2,}")
SHELL_EXPANSION = re.compile(r"\$\{[^}]*\}")


def _is_cp1251_mojibake(line: str) -> bool:
    """True when a line is UTF-8 text that was decoded as CP1251 and re-encoded as UTF-8.

    The transformation preserves bytes, so it is exactly invertible: encoding back to CP1251 must
    succeed and decoding the result as UTF-8 must yield *different* text. This is deliberately not
    a marker list. Genuine typography (`—`, `§`) and genuine Cyrillic both survive the test,
    because their CP1251 bytes are not valid UTF-8 — so it catches corruption without a list that
    needs maintaining and that would fire on legitimate Ukrainian apostrophes (`зв'язок`).
    """
    if not any(ord(ch) > 127 for ch in line):
        return False
    try:
        return line.encode("cp1251").decode("utf-8") != line
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _lost_character_runs(line: str) -> list[str]:
    """`?` runs that are not a shell pattern — the signature of a lossy transcode."""
    return [match.group(0) for match in QUESTION_RUN.finditer(SHELL_EXPANSION.sub("", line))]


def _text_files() -> tuple[list[Path], str]:
    """Every text file we own, and how the set was derived.

    ``git ls-files --cached --others --exclude-standard`` is preferred because it uses the
    project's own ``.gitignore`` to decide what belongs to the repository — it excludes `.env`
    and the `ui` submodule for free. It falls back to a tree walk when git is unavailable, since
    this script must run before anything at all is installed.
    """
    basis = "git ls-files"
    candidates: list[Path]
    # `shutil.which` rather than a bare "git": it yields an absolute path, so the call does not
    # depend on the current directory's PATH being trusted, and a missing git is handled without
    # relying on an exception. Hard-coding C:\... or /usr/bin/git would break the other platform.
    git = shutil.which("git")
    if git is None:
        basis = "tree walk (git not on PATH)"
        candidates = [path for path in REPO_ROOT.rglob("*") if path.is_file()]
    else:
        try:
            result = subprocess.run(  # noqa: S603 - absolute path from which(), literal argv
                [git, "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            candidates = [REPO_ROOT / line for line in result.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.CalledProcessError) as exc:
            basis = f"tree walk (git failed: {type(exc).__name__})"
            candidates = [path for path in REPO_ROOT.rglob("*") if path.is_file()]

    files: list[Path] = []
    for path in candidates:
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in ENCODING_SKIP_PARTS for part in path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files, basis


def check_encoding(report: Report) -> None:
    """Every text file must be valid UTF-8, and free of mojibake and lost characters."""
    files, basis = _text_files()
    report.checks["encoding_basis"] = basis
    report.checks["text_files_scanned"] = len(files)

    if not files:
        report.fail("no text files were found — the encoding checker's discovery is stale")
        return

    for path in sorted(files):
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            report.fail(f"{rel} is not valid UTF-8 (byte {exc.start}: {exc.reason})")
            continue
        except OSError as exc:  # pragma: no cover - a file vanishing mid-run
            report.fail(f"{rel} could not be read: {exc}")
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            if _is_cp1251_mojibake(line):
                report.fail(
                    f"{rel}:{number} contains CP1251 mojibake — the text was re-encoded: "
                    f"{line.strip()[:90]}"
                )
            lost = _lost_character_runs(line)
            if lost:
                report.fail(
                    f"{rel}:{number} contains {lost[0]!r} — a '?' where a character was lost "
                    f"(a lossy transcode): {line.strip()[:90]}"
                )


def _service_names(compose: str) -> set[str]:
    """Service names: two-space-indented keys under a top-level ``services:``.

    Scoped to that block on purpose. Matching indented keys across the whole file also picks up the
    children of ``networks:`` and ``volumes:``, which is how the first version of this check
    reported the overlay's `llm-net` as a service that does not exist.
    """
    match = re.search(r"^services:\n(.*?)(?=^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    if not match:
        return set()
    return set(re.findall(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):", match.group(1), re.MULTILINE))


def check_overlay(report: Report) -> None:
    """The server overlay must only override services that actually exist.

    An override naming a service the base file does not define is not an error to Compose — it is
    simply never applied. That is the quietest possible failure: the overlay looks deliberate, the
    stand comes up, and one setting is missing. The same class covers a wrong *network* name, which
    Compose cannot catch for us at all, so this is the cheap half of that check.
    """
    if not SERVER_COMPOSE.is_file():
        report.note("no server overlay present; skipping the overlay check")
        return

    base_services = _service_names(DEV_COMPOSE.read_text(encoding="utf-8"))
    overlay_services = _service_names(SERVER_COMPOSE.read_text(encoding="utf-8"))

    unknown = sorted(overlay_services - base_services)
    if unknown:
        report.fail(
            f"{SERVER_COMPOSE.name} overrides services that do not exist in "
            f"{DEV_COMPOSE.name}, so those overrides would silently do nothing: {unknown}"
        )
    report.checks["overlay_services"] = sorted(overlay_services)


def run_checks() -> Report:
    report = Report()
    check_ports(report)
    check_env_example(report)
    check_secrets(report)
    check_encoding(report)
    check_overlay(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = run_checks()
    if args.json:
        json.dump(
            {
                "ok": not report.failures,
                "failures": report.failures,
                "notes": report.notes,
                "checks": report.checks,
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        ports = cast("list[str]", report.checks.get("published_ports", []))
        services = cast("list[str]", report.checks.get("internal_services", []))
        documented = cast("list[str]", report.checks.get("documented_variables", []))
        aliases = cast("list[str]", report.checks.get("settings_aliases", []))
        scanned = report.checks.get("text_files_scanned", 0)
        basis = report.checks.get("encoding_basis", "unknown")
        print("Phase 0 environment audit")
        print("=" * 60)
        print(f"published ports   : {', '.join(ports) or 'none'}")
        print(f"internal services : {', '.join(services)}")
        print(f"documented vars   : {len(documented)}")
        print(f"settings aliases  : {len(aliases)}")
        print(f"text files checked: {scanned} ({basis})")
        for note in report.notes:
            print(f"note    : {note}")
        if report.failures:
            print("-" * 60)
            for failure in report.failures:
                print(f"FAIL    : {failure}")
            print(f"\n{len(report.failures)} problem(s) found")
            return 1
        print("-" * 60)
        print("OK      : loopback-only ports, complete .env.example, no committed secret,")
        print("          valid UTF-8 with no transcoding damage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
