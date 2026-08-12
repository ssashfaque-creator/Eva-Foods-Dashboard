import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

type ChatMessage = {
  role: string;
  content: string;
  followup?: Record<string, unknown> | null;
};

function bridgeConfig() {
  const url = (process.env.EVA_BRIDGE_URL || "").replace(/\/$/, "");
  const secret = process.env.EVA_BRIDGE_SECRET || "";
  return { url, secret };
}

export async function POST(req: NextRequest) {
  const { url, secret } = bridgeConfig();
  if (!url || !secret) {
    return NextResponse.json(
      {
        ok: false,
        error:
          "Server misconfigured. Set EVA_BRIDGE_URL and EVA_BRIDGE_SECRET in Vercel.",
      },
      { status: 503 }
    );
  }

  let body: {
    messages?: ChatMessage[];
    model?: string;
    reply_followup?: Record<string, unknown> | null;
  };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json(
      { ok: false, error: "Invalid JSON body" },
      { status: 400 }
    );
  }

  const messages = Array.isArray(body.messages) ? body.messages : [];
  if (!messages.length) {
    return NextResponse.json(
      { ok: false, error: "messages required" },
      { status: 400 }
    );
  }

  const payload = JSON.stringify({
    messages,
    model: body.model || undefined,
    reply_followup: body.reply_followup || undefined,
  });

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${secret}`,
  };

  // Prefer SSE status stream so long ReAct rounds stay alive / visible.
  try {
    const upstream = await fetch(`${url}/chat/stream`, {
      method: "POST",
      headers,
      body: payload,
      signal: AbortSignal.timeout(55_000),
    });

    if (upstream.ok && upstream.body) {
      // Proxy SSE through to the browser
      return new NextResponse(upstream.body, {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    // Fall back to classic /chat if stream route missing (older bridge)
    if (upstream.status === 404) {
      const classic = await fetch(`${url}/chat`, {
        method: "POST",
        headers,
        body: payload,
        signal: AbortSignal.timeout(55_000),
      });
      const data = await classic.json().catch(() => ({}));
      if (!classic.ok) {
        return NextResponse.json(
          {
            ok: false,
            error:
              (data && (data.detail || data.error)) ||
              `Bridge error (${classic.status})`,
          },
          { status: classic.status }
        );
      }
      return NextResponse.json(data);
    }

    const data = await upstream.json().catch(() => ({}));
    return NextResponse.json(
      {
        ok: false,
        error:
          (data && (data.detail || data.error)) ||
          `Bridge error (${upstream.status})`,
      },
      { status: upstream.status }
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Bridge unreachable";
    return NextResponse.json(
      {
        ok: false,
        error:
          "Cannot reach your Mac bridge. Is `eva-dashboard bridge` running and Cloudflare Tunnel up? " +
          msg,
      },
      { status: 502 }
    );
  }
}
