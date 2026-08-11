"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

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

/** Light markdown → HTML for assistant replies (keeps Eva HTML tables). */
function renderAssistantHtml(raw: string): string {
  if (!raw) return "";
  // Already contains our table HTML — keep blocks, escape the rest loosely
  if (raw.includes("<table") || raw.includes("eva-mtx")) {
    return raw
      .split(/(<div[\s\S]*?<\/div>|<table[\s\S]*?<\/table>)/gi)
      .map((chunk) => {
        if (
          /^<div/i.test(chunk) ||
          /^<table/i.test(chunk)
        ) {
          return chunk;
        }
        return formatPlainMarkdown(chunk);
      })
      .join("");
  }
  return formatPlainMarkdown(raw);
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
    const rows = tableBuf.filter((r) => !/^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$/.test(r));
    out.push("<table>");
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
    out.push("</table>");
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

export default function HomePage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

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
  }, [messages, busy]);

  const statusLabel = useMemo(() => {
    if (online === null) return "Checking…";
    if (online) return "Mac linked";
    return "Mac offline";
  }, [online]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setError(null);
    setInput("");
    if (taRef.current) taRef.current.style.height = "auto";

    const next: ChatMessage[] = [
      ...messages,
      { role: "user", content: trimmed },
    ];
    setMessages(next);
    setBusy(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next }),
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
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand">Eva Foods</div>
          <div className="tagline">Live sales analyst</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
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
            <p className="hero-brand">Eva Foods</p>
            <h1>Ask your live numbers.</h1>
            <p>
              Sales, AMS, Price Fetch, parties — from the database on your Mac.
              No API key on this phone.
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
                <div className="bubble user">{m.content}</div>
              ) : (
                <div
                  className="bubble assistant"
                  dangerouslySetInnerHTML={{
                    __html: renderAssistantHtml(m.content),
                  }}
                />
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
        <form className="composer-inner" onSubmit={onSubmit}>
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder="Ask Eva…"
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
    </div>
  );
}
