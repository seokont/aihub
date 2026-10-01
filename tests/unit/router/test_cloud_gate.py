"""The cloud-egress gate's structural guards (task 2.4; closes BACKLOG item 5).

`moni_router.policy` and `moni_router.provider` both *declare*, in their docstrings, that
`provider_from_config` is called from exactly one place and that the endpoint and the credential
are named only in the gateway's `Settings` — and both named this file as the thing that keeps it
true. It did not exist, so those sentences were aspirations. A docstring that names a guard which
is not there is worse than no claim at all: the next reader checks, finds nothing, and stops
believing the surrounding prose too.

**Why these are structural rather than behavioural.** Behaviour tests answer "does this call do
the right thing?"; they cannot express "there is no second way to do this". A second egress path
that nothing currently calls passes every behavioural test in the repository, and the change
that wires it up is the one that leaks. So these tests read the tree.

**What is asserted, and what is deliberately not.**

* the **factory** has exactly one call site in the application — `policy._provider_for`;
* the **endpoint and credential names** are declared as `Settings` aliases in one place, the
  gateway's `config.py`, so "where does this deployment read the cloud address from?" has one
  answer;
* the **implementation** (`OpenAICompatibleCloudProvider`) is named only where it is built;
* the **router hard-codes no address at all** — the base URL is configuration or nothing.

Tests are excluded from the scan on purpose: `tests/unit/router/test_policy.py` calls the factory
directly, and it should — that is how the factory's own contract is pinned. The property under
test is about *application* code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: The application source trees. Tests are deliberately absent — see the module docstring.
APP_SOURCE_ROOTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "gateway" / "src",
    REPO_ROOT / "agent" / "src",
    REPO_ROOT / "router" / "src",
    REPO_ROOT / "ingest" / "src",
    *(sorted((REPO_ROOT / "mcp").glob("*/src"))),
)

#: Every name a deployment could use to point this stack at a cloud endpoint. They are `Settings`
#: aliases; nothing else may bind them.
CLOUD_SETTINGS_ALIASES: Final[frozenset[str]] = frozenset(
    {"CLOUD_PROVIDER", "CLOUD_BASE_URL", "CLOUD_API_KEY", "CLOUD_MODEL"}
)

POLICY_MODULE: Final = "router/src/moni_router/policy.py"
PROVIDER_MODULE: Final = "router/src/moni_router/provider.py"
SETTINGS_MODULE: Final = "gateway/src/moni_gateway/config.py"


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _app_modules() -> list[tuple[Path, ast.Module]]:
    """Every application module, parsed. Excludes tests by construction of the roots."""
    modules: list[tuple[Path, ast.Module]] = []
    for root in APP_SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            modules.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return modules


def _referenced_name(node: ast.expr) -> str | None:
    """``f`` for ``f(...)``, ``cls`` for ``mod.cls(...)`` — the name a call or reference uses."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_the_scan_reaches_the_application_sources() -> None:
    """Anti-vacuity: a wrong root would make every guard below pass while reading nothing.

    Every assertion in this file is of the form "this set is exactly small", and an empty scan
    satisfies all of them. So the scan itself is asserted to reach the modules the guards are
    about.
    """
    modules = _app_modules()
    names = {_relative(path) for path, _ in modules}

    assert len(modules) >= 40, f"the scan found only {len(modules)} application modules"
    assert {POLICY_MODULE, PROVIDER_MODULE, SETTINGS_MODULE} <= names, (
        "the scan did not reach modules these guards are about: "
        f"{sorted({POLICY_MODULE, PROVIDER_MODULE, SETTINGS_MODULE} - names)}"
    )


def test_provider_from_config_is_called_from_exactly_one_place() -> None:
    """The factory is the only way a cloud client is built, so its call sites are the egress paths.

    A call is an AST ``Call`` whose function name is ``provider_from_config`` — not a substring
    match, which would also fire on the definition, the import and every mention in a docstring.
    The docstring case is the one that matters: this module's own subject is a name that appears
    in prose in three modules.
    """
    call_sites: list[str] = []
    for path, tree in _app_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _referenced_name(node.func) == "provider_from_config":
                call_sites.append(_relative(path))

    assert call_sites == [POLICY_MODULE], (
        "a cloud client may be built from exactly one place, and that place decides *whether* the "
        f"call may reach the cloud at all; found call sites: {call_sites}"
    )


def test_the_cloud_endpoint_and_credential_are_named_only_where_settings_declares_them() -> None:
    """One answer to "where does this deployment read the cloud address from?".

    Asserted through the ``alias=`` keyword rather than by scanning text, because
    `provider.py` legitimately *mentions* the names inside an error message ("an empty
    CLOUD_BASE_URL/CLOUD_API_KEY means 'no cloud'"). A substring test could not tell a
    diagnostic from a binding, and loosening it to accommodate the message would stop it
    detecting a second binding.
    """
    bound: dict[str, set[str]] = {}
    for path, tree in _app_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg not in {"alias", "validation_alias"}:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and value.value in CLOUD_SETTINGS_ALIASES:
                bound.setdefault(_relative(path), set()).add(str(value.value))

    assert set(bound) == {SETTINGS_MODULE}, (
        f"only {SETTINGS_MODULE} may declare the cloud settings; found {sorted(bound)}"
    )
    assert bound[SETTINGS_MODULE] == CLOUD_SETTINGS_ALIASES, (
        "every cloud setting must be declared together, or a half-configured deployment becomes "
        f"expressible: {sorted(CLOUD_SETTINGS_ALIASES - bound[SETTINGS_MODULE])} missing"
    )


def test_the_provider_implementation_is_named_only_where_it_is_built() -> None:
    """Nothing else constructs a provider, so the single-call-site property is not circumvented."""
    referenced: set[str] = set()
    for path, tree in _app_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                if _referenced_name(node) == "OpenAICompatibleCloudProvider":
                    referenced.add(_relative(path))
            elif isinstance(node, ast.Constant) and node.value == "OpenAICompatibleCloudProvider":
                # `__all__` re-exports the name; that is a declaration, not a construction, but it
                # must still live in the module that owns the class.
                referenced.add(_relative(path))

    assert referenced == {PROVIDER_MODULE}, (
        "the concrete cloud provider is built in one module and named nowhere else; found "
        f"{sorted(referenced)}"
    )


def test_the_router_hard_codes_no_cloud_endpoint_address() -> None:
    """The address is configuration. A literal in the router would be an endpoint nobody set.

    Checked with the AST rather than a text search, so a URL inside a *message* is still a
    finding here while a relative path constant (`/chat/completions`) is not: the point is that
    no absolute address is pinned in code, and relative paths are protocol, not destinations.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "router" / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("http://", "https://")):
                    offenders.append(f"{_relative(path)}:{node.lineno}")
    assert offenders == [], f"the router pins an endpoint address: {offenders}"
