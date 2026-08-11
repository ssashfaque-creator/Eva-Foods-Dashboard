import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

type ChatMessage = { role: string; content: string };

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

  let body: { messages?: ChatMessage[]; model?: string };
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

  try {
    const upstream = await fetch(`${url}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${secret}`,
      },
      body: JSON.stringify({
        messages,
        model: body.model || undefined,
      }),
      // Phone chats can take a while (tool rounds)
      signal: AbortSignal.timeout(55_000),
    });

    const data = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      return NextResponse.json(
        {
          ok: false,
          error:
            (data && (data.detail || data.error)) ||
            `Bridge error (${upstream.status})`,
        },
        { status: upstream.status }
      );
    }
    return NextResponse.json(data);
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
