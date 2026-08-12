"""Central Memory Context for multi-turn query state.

Replaces the heuristic pile-up of soft-stick / forced_prior / coerce-to-prior
guesses. The LLM declares ``state_action`` ∈ {keep, modify, clear} on each
QuerySpec; Python applies that decision against a strict JSON memory object.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Maps LLM state_action → internal base / context_handling
STATE_ACTION_TO_BASE = {
    "keep": "prior",
    "modify": "prior",
    "clear": "none",
}

BASE_TO_STATE_ACTION = {
    "prior": "modify",  # legacy prior without explicit keep → treat as modify
    "none": "clear",
}


@dataclass
class MemoryContext:
    """Strict JSON representation of the current active filters and grain."""

    filters: dict[str, Any] = field(default_factory=dict)
    party_scope: dict[str, Any] = field(default_factory=dict)
    business_units: list[str] = field(default_factory=list)
    row_dimensions: list[str] = field(default_factory=list)
    column_dimensions: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    period: dict[str, Any] | None = None
    period_phrase: str | None = None
    months_back: int | None = None
    excludes: dict[str, Any] = field(default_factory=dict)
    operation: str | None = None
    source: str = "none"
    last_user_text: str = ""

    def is_empty(self) -> bool:
        return not any(
            [
                self.filters,
                self.party_scope,
                self.business_units,
                self.row_dimensions,
                self.metrics,
                self.period,
                self.period_phrase,
                self.excludes,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop empties for prompt compactness
        out: dict[str, Any] = {}
        for k, v in d.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out

    def to_prior_dict(self) -> dict[str, Any] | None:
        """Shape expected by merge_prior_into_spec / execute_query_spec."""
        if self.is_empty():
            return None
        return {
            "source": self.source or "memory",
            "filters": dict(self.filters),
            "party_scope": dict(self.party_scope) or None,
            "business_units": list(self.business_units) or None,
            "row_dimensions": list(self.row_dimensions) or None,
            "column_dimensions": list(self.column_dimensions) or None,
            "metrics": list(self.metrics) or None,
            "row_dimension": (
                self.row_dimensions[-1] if self.row_dimensions else None
            ),
            "column_dimension": (
                self.column_dimensions[0] if self.column_dimensions else None
            ),
            "months_back": self.months_back,
            "period_phrase": self.period_phrase,
            "period": self.period,
            "excludes": dict(self.excludes) or None,
            "operation": self.operation,
            "intent_hint": self.operation,
        }

    def to_prompt_block(self) -> str:
        """Strict JSON memory block for the system prompt (no HTML)."""
        if self.is_empty():
            return (
                "MEMORY_CONTEXT: none\n"
                "Fresh ask → state_action='clear' (or context_handling='none')."
            )
        payload = self.to_dict()
        party_hint = ""
        if self.party_scope:
            party_hint = (
                "- CUSTOMER SCOPE ACTIVE: "
                f"{json.dumps(self.party_scope, default=str)}. "
                "Short follow-ups (price / % AMS / last purchase) → "
                "state_action='keep', clear_filters=[], KEEP this party scope.\n"
            )
        return (
            "MEMORY_CONTEXT (active filters + grain — JSON only):\n"
            f"{json.dumps(payload, indent=2, default=str)}\n\n"
            "State rules (STRICT — prefer state_action over guessing):\n"
            f"{party_hint}"
            "- state_action='keep' → reuse ALL active filters/grain; only change "
            "metrics/period/rows when the user asks. clear_filters=[] unless "
            "dropping a named filter.\n"
            "- state_action='modify' → reuse prior filters, then apply "
            "clear_filters + new filters patches (city swap, exclude party, …).\n"
            "- state_action='clear' → ignore MEMORY_CONTEXT; fresh complete ask.\n"
            "- Short customer follow-ups (price / % AMS / last purchase) → "
            "state_action='keep' and KEEP party_scope.\n"
            "- 'remove/exclude X from this' → state_action='modify' + excludes.\n"
            "- ONLY when user asks national / all Pakistan / other cities "
            "(without naming the city again) → state_action='modify', "
            "clear_filters:['city']. If they still say 'in Lahore', KEEP "
            "filters.city='Lahore' and do NOT put city in clear_filters.\n"
            "- Complete ask that restates city + brands (e.g. 'Eva Consumer "
            "and Eva Bulk in Lahore') → prefer state_action='clear' with "
            "filters.city + business_units set explicitly.\n"
            "- Legacy aliases: context_handling='prior'≡modify/keep; "
            "'none'≡clear.\n"
        )

    @classmethod
    def from_prior_dict(
        cls,
        prior: dict[str, Any] | None,
        *,
        last_user_text: str = "",
    ) -> MemoryContext:
        if not prior:
            return cls(last_user_text=last_user_text)
        filters = dict(prior.get("filters") or {})
        party_scope = dict(prior.get("party_scope") or {})
        if not party_scope:
            for key in ("party", "parties", "party_ilike"):
                if filters.get(key) not in (None, "", []):
                    party_scope[key] = filters[key]
        rows = [r for r in list(prior.get("row_dimensions") or []) if r]
        if not rows and prior.get("row_dimension"):
            groups = [g for g in list(prior.get("row_groups") or []) if g]
            rows = groups + [str(prior["row_dimension"])]
        cols = [c for c in list(prior.get("column_dimensions") or []) if c]
        if not cols and prior.get("column_dimension"):
            cols = [str(prior["column_dimension"])]
        period = prior.get("period") if isinstance(prior.get("period"), dict) else None
        return cls(
            filters=filters,
            party_scope=party_scope,
            business_units=[
                str(b) for b in (prior.get("business_units") or []) if b
            ],
            row_dimensions=rows,
            column_dimensions=cols,
            metrics=[str(m) for m in (prior.get("metrics") or []) if m],
            period=period,
            period_phrase=(
                str(prior.get("period_phrase") or "").strip() or None
            ),
            months_back=(
                int(prior["months_back"])
                if prior.get("months_back") is not None
                else None
            ),
            excludes=dict(prior.get("excludes") or {}),
            operation=str(
                prior.get("operation")
                or prior.get("intent_hint")
                or prior.get("kind")
                or ""
            ).strip()
            or None,
            source=str(prior.get("source") or "prior"),
            last_user_text=last_user_text or "",
        )

    @classmethod
    def from_query_state(
        cls,
        state: dict[str, Any] | None,
        *,
        last_user_text: str = "",
    ) -> MemoryContext:
        if not state:
            return cls(last_user_text=last_user_text)
        from eva_dashboard.query_spec import prior_context_from_query_state

        prior = prior_context_from_query_state(state)
        return cls.from_prior_dict(prior, last_user_text=last_user_text)


def resolve_state_action(spec: dict[str, Any]) -> str:
    """Canonical state_action from a (possibly legacy) QuerySpec."""
    explicit = str(spec.get("state_action") or "").strip().lower()
    if explicit in STATE_ACTION_TO_BASE:
        return explicit
    base = str(spec.get("base") or spec.get("context_handling") or "none").strip().lower()
    if base == "prior":
        # keep if no clear and filters empty patch; else modify
        clear = list(spec.get("clear") or spec.get("clear_filters") or [])
        new_filters = {
            k: v
            for k, v in dict(spec.get("filters") or {}).items()
            if v not in (None, "", [])
        }
        if not clear and not new_filters and not spec.get("excludes"):
            return "keep"
        return "modify"
    return "clear"


def apply_state_action_to_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize state_action ↔ base so merge_prior / soft-stick agree."""
    out = dict(spec)
    action = resolve_state_action(out)
    out["state_action"] = action
    out["base"] = STATE_ACTION_TO_BASE[action]
    out["context_handling"] = out["base"]
    return out


def merge_memory_into_spec(
    spec: dict[str, Any],
    memory: MemoryContext | None,
) -> dict[str, Any]:
    """Apply MemoryContext using explicit state_action (no soft guessing).

    - clear → return spec unchanged (no prior merge)
    - keep / modify → merge via merge_prior_into_spec
    """
    from eva_dashboard.query_spec import merge_prior_into_spec

    out = apply_state_action_to_spec(spec)
    action = out["state_action"]
    if action == "clear" or memory is None or memory.is_empty():
        # Ensure base=none so downstream stick heuristics don't revive prior
        out["base"] = "none"
        out["context_handling"] = "none"
        return out
    prior = memory.to_prior_dict()
    out["base"] = "prior"
    out["context_handling"] = "prior"
    return merge_prior_into_spec(out, prior)
