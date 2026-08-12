# ReAct Multi-Step Agent (v1.4)

Eva Chat can answer **open-ended** commercial questions via a multi-step
ReAct loop (`run_agent_loop`) with four tool families:

| Tool | Use |
|------|-----|
| `run_standard_analytics_pivot` | Volume / AMS / ranks / Price Fetch via existing engines |
| `execute_read_only_sql` | Novel SELECT asks (min/max, who-at-rate, dispersion) |
| `calculate_expression` | Sandboxed math (`rate * 24.7 / 6`) |
| `get_database_schema` / `lookup_entity_values` | Schema + entity discovery |

## Feature flag

- **On by default:** `EVA_REACT_AGENT=1`
- Disable (legacy single `plan_query` loop): `EVA_REACT_AGENT=0`

Who-is / exclude / ordinal fast paths still bypass the agent for speed.

## Files

- `eva_dashboard/tools/` — SQL, calculator, discovery, legacy adapter
- `eva_dashboard/agent_loop.py` — `run_agent_loop`, `dispatch_react_tool`
- `eva_dashboard/chatbot.py` — wires Streamlit / bridge chat into ReAct

## Mac

```bash
curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/main/scripts/update.sh" | bash
export OPENAI_API_KEY=sk-...
# optional: export EVA_REACT_AGENT=1
"$HOME/Eva-Foods-Dashboard-new/.venv/bin/eva-dashboard" app --data-dir ~/Documents/EvaFoodsData
```
