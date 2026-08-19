# ReAct Multi-Step Agent (v1.4.5)

Eva Chat answers open-ended commercial questions via:

1. **Commercial briefing** — live DB snapshot, QuerySpec contract, vocabulary, metrics, product language (injected every ReAct turn)
2. **Router** — `standard` / `discovery` / `math` / `clarify` / `mixed`
3. **Personal lexicon** — nicknames, sticky prefs, **price preference** (clarify once → default forever)
4. **Ask grounding** — resolves parties before tools run
5. **Playbooks** — multi-hop recipes (lowest→buyer, rate→math, YoY, exclude, last price, party profile, …)
6. **Tools** — legacy pivots / guarded SQL / calculator
7. **Verifier** — retries bad answers (up to 2×)
8. **Money-metric golden eval** — AMS / volume / sales / Price Fetch must use `run_standard_analytics_pivot`
9. **Human feedback** — 👍/👎 → `eval_failures` table for weekly golden promotion
10. **Bridge SSE** — `/chat/stream` status events for phone latency

Fast paths (skip the model when they succeed): exclude/remove follow-up, **tell me about X** → `party_profile`, **who is X** (identity only) → `party_lookup`. Combined “who is X and show sales” is **not** stolen by who-is.

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
