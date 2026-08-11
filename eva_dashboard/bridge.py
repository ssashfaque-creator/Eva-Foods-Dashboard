"""Phone / Vercel chat bridge — local FastAPI over the live Eva DB + OpenAI key.

Run on the Mac (API key + SQLite stay here). Expose with Cloudflare Tunnel.
The Vercel UI proxies to this service; end users never see the key.
"""

from __future__ import annotations

import json
import os
import secrets
import traceback
from typing import Any

from pydantic import BaseModel, Field

from eva_dashboard import __version__
from eva_dashboard.chatbot import DEFAULT_MODEL, chat_completion, resolve_api_key
from eva_dashboard.db import init_db
from eva_dashboard.paths import db_path

_FOLLOWUP_KEYS = (
    "table_spec",
    "price_spec",
    "party_spec",
    "query_state",
    "export",
)


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    followup: dict[str, Any] | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    model: str | None = None
    reply_followup: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    ok: bool = True
    reply: str
    messages: list[dict[str, Any]]


class ExportRequest(BaseModel):
    followup: dict[str, Any] = Field(default_factory=dict)
    format: str = "xlsx"  # xlsx | pdf


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Coerce tool/export payloads into JSON-safe Python types."""
    if depth > 8:
        return None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
            return None
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            safe = _json_safe(v, depth=depth + 1)
            if safe is not None or v is None:
                out[str(k)] = safe
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, depth=depth + 1) for v in value]
    # numpy / pandas scalars
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item(), depth=depth + 1)
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _sanitize_followup(
    meta: dict[str, Any] | None,
    *,
    keep_export: bool = True,
) -> dict[str, Any] | None:
    """Keep only known follow-up keys; drop huge/unsafe blobs inbound."""
    if not isinstance(meta, dict) or not meta:
        return None
    out: dict[str, Any] = {}
    for key in _FOLLOWUP_KEYS:
        if key not in meta:
            continue
        if key == "export" and not keep_export:
            continue
        safe = _json_safe(meta.get(key))
        if isinstance(safe, dict) and safe:
            out[key] = safe
        elif key != "export" and safe is not None:
            out[key] = safe
    return out or None


def _chat_error_detail(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "incorrect api key" in low or "invalid_api_key" in low or "401" in low:
        return (
            "OpenAI rejected the API key on your Mac. "
            "export a valid OPENAI_API_KEY=sk-… then restart `eva-dashboard bridge`."
        )
    if "rate limit" in low or "429" in low:
        return "OpenAI rate limit hit. Wait a moment and try again."
    if "timeout" in low or "timed out" in low:
        return (
            "OpenAI request timed out. Try a shorter question, or check Mac network."
        )
    if "insufficient_quota" in low or "billing" in low:
        return "OpenAI quota/billing issue on this API key. Check platform.openai.com."
    # Keep detail short for the phone UI
    return text[:500]


def _bridge_secret() -> str:
    """Shared secret for Vercel → Mac. Generate once if missing."""
    env = (os.environ.get("EVA_BRIDGE_SECRET") or "").strip()
    if env:
        return env
    from eva_dashboard.paths import data_root

    path = data_root() / "bridge_secret.txt"
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def create_app():
    """Build the FastAPI application (lazy import so CLI works without fastapi)."""
    try:
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import Response
    except ImportError as exc:
        raise RuntimeError(
            "Phone bridge needs fastapi + uvicorn. Run:\n"
            "  pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27'"
        ) from exc

    app = FastAPI(
        title="Eva Foods Chat Bridge",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    def _check_secret(
        authorization: str | None = None,
        x_eva_bridge_secret: str | None = None,
    ) -> None:
        expected = _bridge_secret()
        provided = (x_eva_bridge_secret or "").strip()
        if not provided and authorization:
            auth = authorization.strip()
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
            else:
                provided = auth
        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/health")
    def health() -> dict[str, Any]:
        init_db()
        key_ok = bool(resolve_api_key())
        return {
            "ok": True,
            "service": "eva-chat-bridge",
            "version": __version__,
            "database": str(db_path()),
            "openai_key_configured": key_ok,
        }

    @app.get("/ready")
    def ready(
        authorization: str | None = Header(default=None),
        x_eva_bridge_secret: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Authenticated readiness for the Vercel proxy."""
        _check_secret(authorization, x_eva_bridge_secret)
        h = health()
        if not h["openai_key_configured"]:
            raise HTTPException(
                status_code=503,
                detail="OPENAI_API_KEY is not set on the Mac bridge",
            )
        return h

    @app.post("/chat", response_model=ChatResponse)
    def chat(
        payload: ChatRequest,
        authorization: str | None = Header(default=None),
        x_eva_bridge_secret: str | None = Header(default=None),
    ) -> ChatResponse:
        _check_secret(authorization, x_eva_bridge_secret)
        api_key = resolve_api_key()
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "OpenAI API key missing on Mac. "
                    "export OPENAI_API_KEY=sk-… then restart the bridge."
                ),
            )
        init_db()
        history: list[dict[str, Any]] = []
        for m in payload.messages:
            if m.role not in {"user", "assistant"}:
                continue
            entry: dict[str, Any] = {
                "role": m.role,
                "content": m.content or "",
            }
            # Inbound: keep specs for Reply continuity; drop export blob
            # (rebuilt on Mac) so phone payloads stay small/safe.
            follow = _sanitize_followup(m.followup, keep_export=False)
            if follow:
                entry["_eva_followup"] = follow
            history.append(entry)
        if not history or history[-1]["role"] != "user":
            raise HTTPException(
                status_code=400, detail="Last message must be from the user"
            )
        model = (payload.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        reply_meta = (
            _sanitize_followup(payload.reply_followup, keep_export=False) or {}
        )
        try:
            reply, updated = chat_completion(
                history,
                api_key=api_key,
                model=model,
                forced_prior_spec=reply_meta.get("table_spec"),
                forced_prior_price_spec=reply_meta.get("price_spec"),
                forced_prior_party_spec=reply_meta.get("party_spec"),
                forced_query_state=reply_meta.get("query_state"),
            )
        except Exception as exc:  # noqa: BLE001
            print("BRIDGE /chat error:", _chat_error_detail(exc), flush=True)
            traceback.print_exc()
            raise HTTPException(
                status_code=502, detail=_chat_error_detail(exc)
            ) from exc

        safe_msgs: list[dict[str, Any]] = []
        for m in updated:
            role = m.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = m.get("content") or ""
            if not str(content).strip() and role == "assistant":
                continue
            item: dict[str, Any] = {"role": role, "content": str(content)}
            follow = _sanitize_followup(
                m.get("_eva_followup"), keep_export=True
            )
            if follow:
                # Ensure wire-safe JSON (no numpy leftovers)
                try:
                    item["followup"] = json.loads(json.dumps(follow))
                except (TypeError, ValueError):
                    item["followup"] = _sanitize_followup(
                        follow, keep_export=False
                    )
            safe_msgs.append(item)

        try:
            return ChatResponse(ok=True, reply=reply or "", messages=safe_msgs)
        except Exception as exc:  # noqa: BLE001
            print("BRIDGE /chat response build error:", exc, flush=True)
            traceback.print_exc()
            # Still return the text reply without followup metadata
            bare = [
                {"role": m["role"], "content": m["content"]} for m in safe_msgs
            ]
            return ChatResponse(ok=True, reply=reply or "", messages=bare)

    @app.post("/export")
    def export_table(
        payload: ExportRequest,
        authorization: str | None = Header(default=None),
        x_eva_bridge_secret: str | None = Header(default=None),
    ) -> Response:
        """Excel / PDF export for a phone-chat follow-up payload."""
        _check_secret(authorization, x_eva_bridge_secret)
        init_db()
        follow = payload.followup if isinstance(payload.followup, dict) else {}
        if not follow:
            raise HTTPException(status_code=400, detail="followup required")
        fmt = (payload.format or "xlsx").strip().lower()
        try:
            from eva_dashboard.table_export import (
                export_excel_from_followup,
                export_pdf_from_followup,
            )

            if fmt == "pdf":
                out = export_pdf_from_followup(follow)
                if not out:
                    raise HTTPException(status_code=422, detail="Nothing to export")
                data, filename = out
                return Response(
                    content=data,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )
            out = export_excel_from_followup(follow)
            if not out:
                raise HTTPException(status_code=422, detail="Nothing to export")
            data, filename = out
            return Response(
                content=data,
                media_type=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"'
                },
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def run_bridge(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    data_dir: str | None = None,
) -> None:
    """Start uvicorn for the chat bridge (blocking)."""
    if data_dir:
        os.environ["EVA_DATA_DIR"] = str(data_dir)

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "Phone bridge needs uvicorn. Run:\n"
            "  pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27'"
        ) from exc

    init_db()
    secret = _bridge_secret()
    key_ok = bool(resolve_api_key())
    print(f"Eva Foods Chat Bridge v{__version__}")
    print(f"Listening     : http://{host}:{port}")
    print(f"Database      : {db_path()}")
    print(f"OpenAI key    : {'configured' if key_ok else 'MISSING — set OPENAI_API_KEY'}")
    print(f"Bridge secret : {secret}")
    print()
    print("Keep this process running. Expose it with Cloudflare Tunnel, then")
    print("set Vercel env EVA_BRIDGE_URL + EVA_BRIDGE_SECRET to these values.")
    print()

    uvicorn.run(
        "eva_dashboard.bridge:create_app",
        factory=True,
        host=host,
        port=int(port),
        log_level="info",
    )
