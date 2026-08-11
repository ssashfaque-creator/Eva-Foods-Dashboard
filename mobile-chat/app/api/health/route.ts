import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const url = (process.env.EVA_BRIDGE_URL || "").replace(/\/$/, "");
  const secret = process.env.EVA_BRIDGE_SECRET || "";
  if (!url || !secret) {
    return NextResponse.json({
      ok: false,
      configured: false,
      bridge: "missing env",
    });
  }
  try {
    const res = await fetch(`${url}/ready`, {
      headers: { Authorization: `Bearer ${secret}` },
      signal: AbortSignal.timeout(8_000),
      cache: "no-store",
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json({
      ok: res.ok,
      configured: true,
      bridge: data,
      status: res.status,
    });
  } catch (err) {
    return NextResponse.json({
      ok: false,
      configured: true,
      bridge: "unreachable",
      error: err instanceof Error ? err.message : "error",
    });
  }
}
