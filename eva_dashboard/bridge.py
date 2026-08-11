"""Phone / Vercel chat bridge — local FastAPI over the live Eva DB + OpenAI key.

Run on the Mac (API key + SQLite stay here). Expose with Cloudflare Tunnel.
The Vercel UI proxies to this service; end users never see the key.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from pydantic import BaseModel, Field

from eva_dashboard import __version__
from eva_dashboard.chatbot import DEFAULT_MODEL, chat_completion, resolve_api_key
from eva_dashboard.db import init_db
from eva_dashboard.paths import db_path


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
            if m.followup and isinstance(m.followup, dict):
                entry["_eva_followup"] = m.followup
            history.append(entry)
        if not history or history[-1]["role"] != "user":
            raise HTTPException(
                status_code=400, detail="Last message must be from the user"
            )
        model = (payload.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        reply_meta = payload.reply_followup if isinstance(payload.reply_followup, dict) else {}
        try:
            reply, updated = chat_completion(
                history,
                api_key=api_key,
                model=model,
                forced_prior_spec=reply_meta.get("table_spec"),
                forced_prior_price_spec=reply_meta.get("price_spec"),
                forced_prior_party_spec=reply_meta.get("party_spec"),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        safe_msgs: list[dict[str, Any]] = []
        for m in updated:
            role = m.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = m.get("content") or ""
            if not str(content).strip() and role == "assistant":
                continue
            item: dict[str, Any] = {"role": role, "content": str(content)}
            follow = m.get("_eva_followup")
            if isinstance(follow, dict) and follow:
                item["followup"] = follow
            safe_msgs.append(item)

        return ChatResponse(ok=True, reply=reply or "", messages=safe_msgs)

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
