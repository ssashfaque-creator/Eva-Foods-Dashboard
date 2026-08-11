# Eva Foods — phone chat (Vercel)

Deploy this folder to Vercel. Pair it with `eva-dashboard bridge` on your Mac.

Full instructions: [`../docs/DEPLOY_MOBILE_CHAT.md`](../docs/DEPLOY_MOBILE_CHAT.md)

## Local UI preview

```bash
cd mobile-chat
npm install
export EVA_BRIDGE_URL=http://127.0.0.1:8787
export EVA_BRIDGE_SECRET="$(cat ../data/bridge_secret.txt 2>/dev/null || echo test)"
npm run dev
```

Open http://localhost:3000 (bridge must be running on :8787).
