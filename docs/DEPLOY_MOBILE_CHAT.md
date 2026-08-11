# Eva Foods phone chat — Vercel + Mac bridge

Phone users only see the chat UI. Your Mac holds the SQLite database and the OpenAI API key.

```
Phone  →  Vercel (mobile-chat)  →  Cloudflare Tunnel  →  Mac bridge (:8787)
                                      OPENAI_API_KEY + data/eva.db
```

---

## 1. Update the Mac app

```bash
curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/642189d7fad0db2a3c726ce4ac73eb00d6095627/scripts/update.sh" | bash -s -- "$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203"
cd "$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203"
source .venv/bin/activate
pip install -U 'fastapi>=0.110' 'uvicorn[standard]>=0.27' openai
```

(After this PR lands, the update script already includes those packages.)

Set your OpenAI key (once per shell, or add to `~/.zshrc`):

```bash
export OPENAI_API_KEY="sk-..."
```

---

## 2. Start the bridge on the Mac

```bash
cd "$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203"
source .venv/bin/activate
export OPENAI_API_KEY="sk-..."
eva-dashboard bridge --port 8787
```

Leave this terminal open. It prints a **Bridge secret** (also saved to `data/bridge_secret.txt`). Copy it.

Quick check:

```bash
curl -s http://127.0.0.1:8787/health
```

---

## 3. Expose the Mac with Cloudflare Tunnel (free)

Install cloudflared (one-time):

```bash
brew install cloudflare/cloudflare/cloudflared
```

In a **second** terminal, while the bridge is running:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

Copy the HTTPS URL it prints, e.g. `https://random-words.trycloudflare.com`.

> Keep both terminals running whenever you want the phone chat to work.

---

## 4. Deploy the chat UI to Vercel

### Option A — Vercel website (easiest)

1. Go to [https://vercel.com](https://vercel.com) → **Add New… → Project**.
2. Import `ssashfaque-creator/Eva-Foods-Dashboard` (GitHub).
3. Configure:
   - **Root Directory:** `mobile-chat`  ← click Edit, set this
   - **Framework Preset:** Next.js
   - **Build Command:** `next build` (default)
   - **Output:** default
4. **Environment Variables** (Production + Preview):

   | Name | Value |
   |------|--------|
   | `EVA_BRIDGE_URL` | `https://YOUR-tunnel.trycloudflare.com` (no trailing slash) |
   | `EVA_BRIDGE_SECRET` | the Bridge secret from step 2 |

5. Click **Deploy**.

### Option B — Vercel CLI

```bash
# one-time
npm i -g vercel

cd "$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203/mobile-chat"
vercel login
vercel link   # create/link project; set root to mobile-chat if asked

vercel env add EVA_BRIDGE_URL production
# paste: https://YOUR-tunnel.trycloudflare.com

vercel env add EVA_BRIDGE_SECRET production
# paste: the bridge secret

vercel --prod
```

---

## 5. Use on your phone

1. Open the Vercel URL (e.g. `https://eva-foods-xxx.vercel.app`).
2. Status pill should say **Mac linked** (green).
3. Ask questions — answers come from your Mac DB. No API key on the phone.

Add to Home Screen (iOS Safari → Share → Add to Home Screen) for an app-like icon.

---

## Day-to-day checklist

| Step | Command / action |
|------|------------------|
| 1 | `export OPENAI_API_KEY=sk-…` |
| 2 | `eva-dashboard bridge --port 8787` |
| 3 | `cloudflared tunnel --url http://127.0.0.1:8787` |
| 4 | If the tunnel URL changed, update `EVA_BRIDGE_URL` in Vercel → Redeploy |

Optional: create a **named** Cloudflare tunnel with a fixed hostname so the Vercel env rarely changes ([Cloudflare Zero Trust docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)).

---

## Security notes

- OpenAI key never leaves the Mac.
- Vercel only stores the bridge URL + shared secret.
- Bind stays on `127.0.0.1`; Cloudflare Tunnel is the public entry.
- Rotate the secret anytime: delete `data/bridge_secret.txt`, restart bridge, update Vercel env.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Status: Mac offline | Bridge + tunnel both running? Tunnel URL match Vercel `EVA_BRIDGE_URL`? |
| 401 Unauthorized | `EVA_BRIDGE_SECRET` ≠ Mac secret — copy from bridge startup or `data/bridge_secret.txt` |
| 503 OpenAI key missing | `export OPENAI_API_KEY=sk-…` and restart bridge |
| Tables look fine on Mac Streamlit but slow on phone | Normal — tool rounds can take 10–40s; keep the screen awake |
