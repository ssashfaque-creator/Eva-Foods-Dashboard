# ReAct Multi-Step Agent (v1.4.1)

Eva Chat answers open-ended commercial questions via a multi-step ReAct loop
with **routing + SQL guardrails + answer verification**.

## Pipeline

1. **Router** (`intent_router.route_ask`) — `standard` | `discovery` | `math` | `clarify` | `mixed`
2. **Act** — tools (legacy pivot / SQL / calculator / discovery)
3. **Verify** (`answer_verifier.verify_agent_answer`) — retry up to 2× if the reply misses the ask

| Tool | Use |
|------|-----|
| `run_standard_analytics_pivot` | Volume / AMS / ranks / Price Fetch (engines) |
| `execute_read_only_sql` | Novel SELECT (min/max, who-at-rate); AMS/PF formulas **blocked** |
| `calculate_expression` | Sandboxed math |
| `get_database_schema` / `lookup_entity_values` | Schema + entity discovery |

## Feature flag

- **On by default:** `EVA_REACT_AGENT=1`
- Disable: `EVA_REACT_AGENT=0` (legacy single `plan_query` loop)

## Mac

```bash
curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/main/scripts/update.sh" | bash
export OPENAI_API_KEY=sk-...
"$HOME/Eva-Foods-Dashboard-new/.venv/bin/eva-dashboard" app --data-dir ~/Documents/EvaFoodsData
```
