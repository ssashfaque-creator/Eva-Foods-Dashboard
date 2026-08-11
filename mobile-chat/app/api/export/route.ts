import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

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

  let body: { followup?: Record<string, unknown>; format?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json(
      { ok: false, error: "Invalid JSON body" },
      { status: 400 }
    );
  }

  if (!body.followup || typeof body.followup !== "object") {
    return NextResponse.json(
      { ok: false, error: "followup required" },
      { status: 400 }
    );
  }

  const format = body.format === "pdf" ? "pdf" : "xlsx";

  try {
    const upstream = await fetch(`${url}/export`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${secret}`,
      },
      body: JSON.stringify({ followup: body.followup, format }),
      signal: AbortSignal.timeout(55_000),
    });

    if (!upstream.ok) {
      const data = await upstream.json().catch(() => ({}));
      return NextResponse.json(
        {
          ok: false,
          error:
            (data && (data.detail || data.error)) ||
            `Export failed (${upstream.status})`,
        },
        { status: upstream.status }
      );
    }

    const blob = await upstream.arrayBuffer();
    const disposition =
      upstream.headers.get("Content-Disposition") ||
      `attachment; filename="eva_table.${format}"`;
    const contentType =
      upstream.headers.get("Content-Type") || "application/octet-stream";

    return new NextResponse(blob, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Content-Disposition": disposition,
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Bridge unreachable";
    return NextResponse.json(
      {
        ok: false,
        error: "Cannot reach Mac bridge for export. " + msg,
      },
      { status: 502 }
    );
  }
}
