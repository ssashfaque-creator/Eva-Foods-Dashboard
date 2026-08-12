import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function bridgeConfig() {
  const url = (process.env.EVA_BRIDGE_URL || "").replace(/\/$/, "");
  const secret = process.env.EVA_BRIDGE_SECRET || "";
  return { url, secret };
}

export async function POST(req: NextRequest) {
  const { url, secret } = bridgeConfig();
  if (!url || !secret) {
    return NextResponse.json(
      { ok: false, error: "Server misconfigured." },
      { status: 503 }
    );
  }
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json(
      { ok: false, error: "Invalid JSON body" },
      { status: 400 }
    );
  }
  try {
    const upstream = await fetch(`${url}/feedback`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${secret}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
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
    return NextResponse.json({ ok: false, error: msg }, { status: 502 });
  }
}
