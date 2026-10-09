"""`odoo_search`: the whitelists and the validators. Every guard is here, in code, testable without Odoo.

**Why the guards are not in the prompt.** A prompt asks the model to behave; a validator makes the
behaviour impossible to get wrong. The model in a free-form loop chooses the model name, the fields and
the domain, so those three choices are the attack surface, and each is checked against a list in this
module before a single RPC is built. The loop's design (spike) keeps every guard here for that reason.

**The field table is shared, not copied.** `moni_router.fields` owns which Odoo field names are money,
which are safe to leave as numbers, and which are neither; this module declares the per-model field lists
and their *types*, and the shared guard `check_declared_fields` runs over them, so a numeric field has to
be classified deliberately instead of arriving unprotected. A second copy of the classification is how a
field becomes "whitelisted" in one place and unprotected in the other.

**What is deliberately excluded.** `res.partner` can read a great deal that is level-A-adjacent, so the
first cut excludes `vat`, the bank fields, `email` and `phone` by name -- in `EXCLUDED_PARTNER_FIELDS`
where the exclusion is visible and testable, not by leaving them out of a list silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from moni_router.fields import check_declared_fields, classify

#: The models `odoo_search` may read, with each field's declared type. The declared type is what makes the
#: shared numeric guard meaningful: "numeric" is a property of the declaration, not of the name.
MODELS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "project.task": MappingProxyType(
            {
                "id": "int",
                "name": "str",
                "state": "str",
                "project_id": "int",
                "stage_id": "int",
                "user_id": "int",
                "priority": "int",
                "sequence": "int",
                "planned_hours": "float",
                "effective_hours": "float",
                "remaining_hours": "float",
                "date_deadline": "str",
                "create_date": "str",
                "description": "str",
            }
        ),
        "sale.order": MappingProxyType(
            {
                "id": "int",
                "name": "str",
                "state": "str",
                "partner_id": "int",
                "user_id": "int",
                "amount_untaxed": "float",
                "amount_tax": "float",
                "amount_total": "float",
                "invoice_count": "int",
                "date_order": "str",
                "validity_date": "str",
                "client_order_ref": "str",
            }
        ),
        "res.partner": MappingProxyType(
            {
                "id": "int",
                "name": "str",
                "is_company": "bool",
                "country_id": "int",
                "city": "str",
                "create_date": "str",
            }
        ),
    }
)

#: Fields `res.partner` has and this tool does not read on the first cut: tax ids, bank details, email and
#: phone. Named rather than merely absent, so the exclusion is a decision a reader can see and a test can
#: hold -- and so admitting one later is a visible edit rather than a forgotten omission.
EXCLUDED_PARTNER_FIELDS: Final[frozenset[str]] = frozenset(
    {"vat", "email", "phone", "mobile", "bank_ids", "bank_account_count"}
)

#: The domain operators admitted. A domain is a query language; every operator is surface to validate, so
#: the list is closed and everything outside it is refused by name.
OPERATORS: Final[frozenset[str]] = frozenset(
    {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "child_of"}
)

#: Operators whose right-hand side must be a flat list of scalars.
LIST_OPERATORS: Final[frozenset[str]] = frozenset({"in", "not in"})

#: Caps. A caller asking for more gets the maximum and is told so in the result, which is friendlier than a
#: refusal and still bounded.
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 50
MAX_OFFSET: Final = 10_000
MAX_DOMAIN_TERMS: Final = 8
MAX_LIST_VALUES: Final = 50
MAX_VALUE_CHARS: Final = 200

#: The Python types a domain value may have. `bool` before `int` at the call sites, because `bool` is an
#: `int` in Python and a domain of `True` should not be read as the number 1.
VALUE_TYPES: Final[tuple[type, ...]] = (bool, int, float, str)


class SearchRefused(ValueError):
    """A search the guards refused, naming what was allowed so the caller can correct itself.

    The allowed values are in the message on purpose: a model that asked for a field outside the
    whitelist can only recover if it is told the whitelist, and the whitelist is not a secret — the model
    was already shown the field list in the tool schema.
    """


@dataclass(frozen=True)
class SearchQuery:
    """A validated search: the only shape the Odoo client is ever handed."""

    model: str
    fields: tuple[str, ...]
    domain: tuple[tuple[str, str, Any], ...]
    limit: int
    offset: int

    @property
    def money_fields(self) -> tuple[str, ...]:
        """The requested fields whose values are money, so the caller can seed the anonymiser."""
        return tuple(name for name in self.fields if classify(name) == "money")


def model_fields(model: str) -> Mapping[str, str]:
    """The declared fields of a whitelisted model, or a refusal naming the allowed models."""
    declared = MODELS.get(model)
    if declared is None:
        allowed = ", ".join(sorted(MODELS))
        raise SearchRefused(f"{model!r} is not searchable; the models are: {allowed}")
    return declared


def _check_fields(model: str, requested: Sequence[str] | None) -> tuple[str, ...]:
    declared = model_fields(model)
    if not requested:
        return tuple(declared)
    allowed = set(declared)
    wanted: list[str] = []
    for name in requested:
        if "." in name:
            # A dotted path is a traversal into another record. Refused unless that exact path is
            # whitelisted, which on this cut means refused: an unlisted traversal reads fields nobody
            # classified, and the classification is what this tool's safety rests on.
            raise SearchRefused(
                f"{name!r} is a dotted path; only listed fields may be read: {', '.join(sorted(allowed))}"
            )
        if name not in allowed:
            raise SearchRefused(
                f"{name!r} is not readable on {model}; the fields are: {', '.join(sorted(allowed))}"
            )
        wanted.append(name)
    return tuple(dict.fromkeys(wanted))


def _check_value(field: str, operator: str, value: Any) -> Any:
    if operator in LIST_OPERATORS:
        if not isinstance(value, (list, tuple)):
            raise SearchRefused(
                f"{field} {operator} needs a list of values, got {type(value).__name__}"
            )
        if not value:
            raise SearchRefused(f"{field} {operator} needs at least one value")
        if len(value) > MAX_LIST_VALUES:
            raise SearchRefused(f"{field} {operator} takes at most {MAX_LIST_VALUES} values")
        for item in value:
            if not isinstance(item, VALUE_TYPES):
                raise SearchRefused(
                    f"{field} {operator} got {type(item).__name__}; values must be text, number or boolean"
                )
            if isinstance(item, str) and len(item) > MAX_VALUE_CHARS:
                raise SearchRefused(
                    f"{field} {operator} got a value longer than {MAX_VALUE_CHARS} characters"
                )
        return list(value)

    if not isinstance(value, VALUE_TYPES):
        raise SearchRefused(
            f"{field} {operator} got {type(value).__name__}; values must be text, number or boolean"
        )
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        raise SearchRefused(
            f"{field} {operator} got a value longer than {MAX_VALUE_CHARS} characters"
        )
    return value


def _check_domain(model: str, domain: Sequence[Any] | None) -> tuple[tuple[str, str, Any], ...]:
    if not domain:
        return ()
    declared = model_fields(model)
    if len(domain) > MAX_DOMAIN_TERMS:
        raise SearchRefused(f"at most {MAX_DOMAIN_TERMS} filters are allowed, got {len(domain)}")

    checked: list[tuple[str, str, Any]] = []
    for term in domain:
        if not isinstance(term, (list, tuple)) or len(term) != 3:
            raise SearchRefused(f"each filter must be [field, operator, value]; got {term!r}")
        field, operator, value = term
        if not isinstance(field, str) or field not in declared:
            # A filter on a hidden field would let a caller *probe* it: narrowing by a value you may not
            # read tells you whether it matches. So the domain may only reference readable fields.
            raise SearchRefused(
                f"{field!r} is not readable on {model}, so it cannot be filtered on; "
                f"the fields are: {', '.join(sorted(declared))}"
            )
        if not isinstance(operator, str) or operator not in OPERATORS:
            raise SearchRefused(
                f"{operator!r} is not a supported operator; the operators are: {', '.join(sorted(OPERATORS))}"
            )
        checked.append((field, operator, _check_value(field, operator, value)))
    return tuple(checked)


def _check_window(limit: int | None, offset: int | None) -> tuple[int, int]:
    resolved_limit = DEFAULT_LIMIT if limit is None else int(limit)
    resolved_offset = 0 if offset is None else int(offset)
    if resolved_offset < 0:
        raise SearchRefused("offset cannot be negative")
    # Clamped, not refused: an over-large limit is a caller being optimistic, and the honest answer is the
    # maximum with the count, not an error the caller has to decode.
    return min(max(resolved_limit, 1), MAX_LIMIT), min(resolved_offset, MAX_OFFSET)


def check_models_are_classified() -> None:
    """Run the shared numeric guard over every declared model.

    Called at import time by the tool and by the tests. It is the guarantee the measurement only gave
    evidence for: a numeric field that is neither money nor on the safe list is refused by name, so it
    cannot reach a request unclassified.
    """
    for model, declared in MODELS.items():
        check_declared_fields(model, declared)


def build_query(
    *,
    model: str,
    fields: Sequence[str] | None = None,
    domain: Sequence[Any] | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> SearchQuery:
    """Validate a search request and return the only shape the client is handed.

    The single entry point on purpose: a caller that builds a query another way is a caller that bypassed
    one of the guards, so there is one function and the tool wrapper does nothing but call it.
    """
    return SearchQuery(
        model=model,
        fields=_check_fields(model, fields),
        domain=_check_domain(model, domain),
        limit=_check_window(limit, offset)[0],
        offset=_check_window(limit, offset)[1],
    )


def searchable_models() -> Iterable[str]:
    """The model names, for a tool schema or a refusal message."""
    return tuple(sorted(MODELS))
