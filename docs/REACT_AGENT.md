# ReAct Multi-Step Agent (v1.4.4)

Eva Chat answers open-ended commercial questions via:

1. **Router** — `standard` / `discovery` / `math` / `clarify` / `mixed`
2. **Personal lexicon** — nicknames, sticky prefs, **price preference** (clarify once → default forever)
3. **Ask grounding** — resolves parties before tools run
4. **Playbooks** — multi-hop recipes (lowest→buyer, rate→math, YoY, exclude, last price, …)
5. **Tools** — legacy pivots / guarded SQL / calculator
6. **Verifier** — retries bad answers (up to 2×)
7. **Money-metric golden eval** — AMS / volume / Price Fetch must use `run_standard_analytics_pivot`
8. **Human feedback** — 👍/👎 → `eval_failures` table for weekly golden promotion
9. **Bridge SSE** — `/chat/stream` status events for phone latency

## Defaults

- Model: **`gpt-4o`** (orchestrator). UI still offers mini / 4.1 variants.
- `EVA_REACT_AGENT=1` (default). `0` is **deprecated** rollback only.

Lexicon file: `{data-dir}/personal_lexicon.json`

## Mac

```bash
curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/main/scripts/update.sh" | bash
export OPENAI_API_KEY=sk-...
"$HOME/Eva-Foods-Dashboard-new/.venv/bin/eva-dashboard" app --data-dir ~/Documents/EvaFoodsData
```
