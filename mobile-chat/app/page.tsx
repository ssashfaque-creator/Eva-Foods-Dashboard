"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Followup = {
  table_spec?: Record<string, unknown>;
  price_spec?: Record<string, unknown>;
  party_spec?: Record<string, unknown>;
  export?: unknown;
  [key: string]: unknown;
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  followup?: Followup | null;
};

const FOLLOWUP_MARKER = "[FOLLOW-UP on the answer you just gave]";

const SUGGESTIONS = [
  "How are Eva Consumer sales doing this month?",
  "SKU-wise breakup of al shaheer with price fetch",
  "Metro Habib sales last 6 months",
  "Show LMT sales by packing",
];

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function stripFollowupMarker(text: string): string {
  const t = (text || "").trimStart();
  if (t.startsWith(FOLLOWUP_MARKER)) {
    return t.slice(FOLLOWUP_MARKER.length).replace(/^[\s\n:-]+/, "");
  }
  if (t.toUpperCase().startsWith("[FOLLOW-UP")) {
    return t.replace(/^\[FOLLOW-UP[^\]]*\]\s*/i, "").replace(/^[\s\n:-]+/, "");
  }
  return text || "";
}

function formatPlainMarkdown(text: string): string {
  const escaped = escapeHtml(text);
  const withBold = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const lines = withBold.split("\n");
  const out: string[] = [];
  let inList = false;
  let inTable = false;
  let tableBuf: string[] = [];

  const flushTable = () => {
    if (!tableBuf.length) return;
    const rows = tableBuf.filter(
      (r) => !/^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$/.test(r)
    );
    out.push('<div class="table-block"><div class="table-scroll"><table>');
    rows.forEach((row, i) => {
      const cells = row
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map((c) => c.trim());
      const tag = i === 0 ? "th" : "td";
      out.push(
        "<tr>" +
          cells.map((c) => `<${tag}>${c}</${tag}>`).join("") +
          "</tr>"
      );
    });
    out.push("</table></div></div>");
    tableBuf = [];
    inTable = false;
  };

  for (const line of lines) {
    if (/^\s*\|.+\|\s*$/.test(line)) {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      inTable = true;
      tableBuf.push(line.trim());
      continue;
    }
    if (inTable) flushTable();

    if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${line.replace(/^\s*[-*]\s+/, "")}</li>`);
      continue;
    }
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
    if (!line.trim()) {
      out.push("<br/>");
    } else if (/^###\s+/.test(line)) {
      out.push(`<p><strong>${line.replace(/^###\s+/, "")}</strong></p>`);
    } else {
      out.push(`<p>${line}</p>`);
    }
  }
  if (inTable) flushTable();
  if (inList) out.push("</ul>");
  return out.join("");
}

/** Light markdown → HTML for assistant replies (keeps Eva HTML tables). */
function renderAssistantHtml(raw: string): string {
  if (!raw) return "";
  if (raw.includes("<table") || raw.includes("eva-mtx")) {
    return raw
      .split(/(<div[\s\S]*?<\/div>|<table[\s\S]*?<\/table>)/gi)
      .map((chunk) => {
        if (/^<div/i.test(chunk) || /^<table/i.test(chunk)) {
          if (/^<table/i.test(chunk)) {
            return `<div class="table-block"><div class="table-scroll">${chunk}</div></div>`;
          }
          if (/eva-mtx-wrap/i.test(chunk) || /<table/i.test(chunk)) {
            return `<div class="table-block"><div class="table-scroll">${chunk}</div></div>`;
          }
          return chunk;
        }
        return formatPlainMarkdown(chunk);
      })
      .join("");
  }
  return formatPlainMarkdown(raw);
}

function messageHasTable(content: string): boolean {
  return /<table|^\s*\|.+\|\s*$/m.test(content || "");
}

function canExportFollowup(followup?: Followup | null): boolean {
  if (!followup) return false;
  return Boolean(
    followup.export ||
      followup.table_spec ||
      followup.party_spec ||
      followup.price_spec
  );
}

function csvEscape(cell: string): string {
  const v = cell.replace(/\r?\n/g, " ").trim();
  if (/[",]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}

function tablesToCsv(root: HTMLElement): string | null {
  const tables = Array.from(root.querySelectorAll("table"));
  if (!tables.length) return null;
  const blocks: string[] = [];
  tables.forEach((table, ti) => {
    const rows = Array.from(table.querySelectorAll("tr"));
    const lines = rows.map((tr) =>
      Array.from(tr.querySelectorAll("th,td"))
        .map((cell) => csvEscape(cell.textContent || ""))
        .join(",")
    );
    if (lines.length) {
      if (tables.length > 1) blocks.push(`Table ${ti + 1}`);
      blocks.push(lines.join("\n"));
    }
  });
  return blocks.length ? blocks.join("\n\n") : null;
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function previewText(content: string, max = 72): string {
  const clean = stripFollowupMarker(content)
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (clean.length <= max) return clean;
  return clean.slice(0, max - 1) + "…";
}

export default function HomePage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const assistantRefs = useRef<Record<number, HTMLDivElement | null>>({});

  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      const data = await res.json();
      setOnline(Boolean(data.ok));
    } catch {
      setOnline(false);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 20_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy, replyTo]);

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 2200);
    return () => clearTimeout(id);
  }, [toast]);

  const statusLabel = useMemo(() => {
    if (online === null) return "Checking…";
    if (online) return "Mac linked";
    return "Mac offline";
  }, [online]);

  const replyPreview = useMemo(() => {
    if (replyTo == null) return null;
    const msg = messages[replyTo];
    if (!msg || msg.role !== "assistant") return null;
    return previewText(msg.content);
  }, [messages, replyTo]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setError(null);

    const isFollowup = replyTo != null;
    const followup =
      isFollowup && messages[replyTo!]?.followup
        ? messages[replyTo!].followup
        : null;
    const outboundText = isFollowup
      ? `${FOLLOWUP_MARKER}\n\n${trimmed}`
      : trimmed;

    setInput("");
    setReplyTo(null);
    if (taRef.current) taRef.current.style.height = "auto";

    const next: ChatMessage[] = [
      ...messages,
      { role: "user", content: outboundText },
    ];
    setMessages(next);
    setBusy(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: next.map((m) => ({
            role: m.role,
            content: m.content,
            followup: m.followup || undefined,
          })),
          reply_followup: followup || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || "Chat failed");
      }
      const reply =
        typeof data.reply === "string"
          ? data.reply
          : "No reply from the analyst.";
      if (Array.isArray(data.messages) && data.messages.length) {
        setMessages(
          data.messages
            .filter(
              (m: ChatMessage) =>
                m && (m.role === "user" || m.role === "assistant")
            )
            .map((m: ChatMessage) => ({
              role: m.role,
              content: String(m.content || ""),
              followup: m.followup || null,
            }))
        );
      } else {
        setMessages([...next, { role: "assistant", content: reply }]);
      }
      setOnline(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Something went wrong";
      setError(msg);
      setOnline(false);
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send(input);
  }

  function clearChat() {
    if (busy) return;
    setMessages([]);
    setError(null);
    setReplyTo(null);
  }

  function startReply(index: number) {
    if (busy) return;
    setReplyTo(index);
    setTimeout(() => taRef.current?.focus(), 50);
  }

  function downloadCsv(index: number) {
    const el = assistantRefs.current[index];
    if (!el) {
      setToast("No table found");
      return;
    }
    const csv = tablesToCsv(el);
    if (!csv) {
      setToast("No table found");
      return;
    }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    downloadBlob(
      new Blob([csv], { type: "text/csv;charset=utf-8" }),
      `eva_table_${stamp}.csv`
    );
    setToast("CSV downloaded");
  }

  async function downloadExcel(index: number) {
    const msg = messages[index];
    if (!msg?.followup || !canExportFollowup(msg.followup)) {
      downloadCsv(index);
      return;
    }
    setExporting(`xlsx-${index}`);
    try {
      const res = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ followup: msg.followup, format: "xlsx" }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Excel export failed");
      }
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename="?([^"]+)"?/i.exec(disposition);
      downloadBlob(blob, match?.[1] || "eva_table.xlsx");
      setToast("Excel downloaded");
    } catch (err) {
      const msgText =
        err instanceof Error ? err.message : "Excel export failed";
      setToast(msgText);
      downloadCsv(index);
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand">Eva Foods</div>
          <div className="tagline">Live sales analyst</div>
        </div>
        <div className="top-actions">
          <button
            type="button"
            className="icon-btn"
            onClick={clearChat}
            disabled={busy || messages.length === 0}
          >
            Clear
          </button>
          <div className="status-pill" title="Bridge to your Mac">
            <span className={`status-dot ${online ? "ok" : "bad"}`} />
            {statusLabel}
          </div>
        </div>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <main className="chat-scroll">
        {messages.length === 0 ? (
          <section className="hero-empty">
            <p className="hero-kicker">Sales analyst</p>
            <p className="hero-brand">Eva Foods</p>
            <h1>Ask your live numbers.</h1>
            <p>
              Sales, AMS, Price Fetch, parties — answered from the database on
              your Mac. No API key on this phone.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="suggestion"
                  onClick={() => void send(s)}
                  disabled={busy || online === false}
                >
                  {s}
                </button>
              ))}
            </div>
            {online === false ? (
              <p className="hero-offline">
                Waiting for Mac bridge… start `eva-dashboard bridge` and your
                Cloudflare tunnel.
              </p>
            ) : null}
          </section>
        ) : (
          messages.map((m, i) => (
            <div key={`${m.role}-${i}`} className={`bubble-row ${m.role}`}>
              {m.role === "user" ? (
                <div className="bubble user">
                  {stripFollowupMarker(m.content)}
                </div>
              ) : (
                <>
                  <div
                    className="bubble assistant"
                    ref={(el) => {
                      assistantRefs.current[i] = el;
                    }}
                    dangerouslySetInnerHTML={{
                      __html: renderAssistantHtml(m.content),
                    }}
                  />
                  <div className="msg-actions">
                    <button
                      type="button"
                      className="action-btn primary"
                      onClick={() => startReply(i)}
                      disabled={busy}
                    >
                      ↩ Reply
                    </button>
                    {messageHasTable(m.content) || canExportFollowup(m.followup) ? (
                      <>
                        <button
                          type="button"
                          className="action-btn"
                          onClick={() => downloadCsv(i)}
                          disabled={busy}
                        >
                          ⬇ CSV
                        </button>
                        <button
                          type="button"
                          className="action-btn"
                          onClick={() => void downloadExcel(i)}
                          disabled={busy || exporting === `xlsx-${i}`}
                        >
                          {exporting === `xlsx-${i}` ? "Exporting…" : "⬇ Excel"}
                        </button>
                      </>
                    ) : null}
                  </div>
                </>
              )}
            </div>
          ))
        )}

        {busy ? (
          <div className="bubble-row assistant">
            <div className="bubble assistant">
              <div className="typing" aria-label="Thinking">
                <span />
                <span />
                <span />
              </div>
            </div>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </main>

      <footer className="composer">
        {replyTo != null && replyPreview ? (
          <div className="reply-chip">
            <span>
              <strong>Replying</strong> · {replyPreview}
            </span>
            <button type="button" onClick={() => setReplyTo(null)}>
              Cancel
            </button>
          </div>
        ) : null}
        <form className="composer-inner" onSubmit={onSubmit}>
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder={
              replyTo != null
                ? "Ask a follow-up on this table…"
                : "Ask Eva…"
            }
            disabled={busy}
            onChange={(e) => {
              setInput(e.target.value);
              const el = e.target;
              el.style.height = "auto";
              el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(input);
              }
            }}
          />
          <button
            type="submit"
            className="send-btn"
            disabled={busy || !input.trim()}
            aria-label="Send"
          >
            Send
          </button>
        </form>
      </footer>

      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
