# ReAct Multi-Step Agent (v1.4.2)

Eva Chat answers open-ended commercial questions via:

1. **Router** — `standard` / `discovery` / `math` / `clarify` / `mixed`
2. **Personal lexicon** — learns nicknames (`pepsi` → party) + sticky prefs
3. **Ask grounding** — resolves parties before tools run
4. **Playbooks** — multi-hop recipes (lowest→buyer, rate→math, same-date variance)
5. **Tools** — legacy pivots / guarded SQL / calculator
6. **Verifier** — retries bad answers (up to 2×)

## Feature flag

`EVA_REACT_AGENT=1` (default). Set `0` for legacy single `plan_query` loop.

Lexicon file: `{data-dir}/personal_lexicon.json`

## Mac

```bash
curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/main/scripts/update.sh" | bash
export OPENAI_API_KEY=sk-...
"$HOME/Eva-Foods-Dashboard-new/.venv/bin/eva-dashboard" app --data-dir ~/Documents/EvaFoodsData
```
