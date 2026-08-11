"""Local chat bridge for Vercel / phone UI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eva_dashboard.bridge import _bridge_secret, create_app
from eva_dashboard.db import connect, init_db


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ.pop("EVA_BRIDGE_SECRET", None)
    os.environ.pop("OPENAI_API_KEY", None)


def test_health_and_secret_file() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            secret = _bridge_secret()
            assert len(secret) >= 20
            assert (Path(tmp) / "data" / "bridge_secret.txt").exists()

            app = create_app()
            client = TestClient(app)
            h = client.get("/health")
            assert h.status_code == 200
            assert h.json()["ok"] is True
            assert h.json()["openai_key_configured"] is False

            denied = client.get("/ready")
            assert denied.status_code == 401

            ready = client.get(
                "/ready", headers={"Authorization": f"Bearer {secret}"}
            )
            assert ready.status_code == 503  # no OpenAI key
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
            os.environ.pop("EVA_BRIDGE_SECRET", None)


def test_chat_requires_secret_and_returns_reply() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["EVA_BRIDGE_SECRET"] = "test-secret-token"
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
                    "datetime('now'))"
                )
                conn.commit()

            app = create_app()
            client = TestClient(app)

            with patch("eva_dashboard.bridge.chat_completion") as mock_chat:
                mock_chat.return_value = (
                    "Hello from Eva",
                    [
                        {"role": "user", "content": "hi"},
                        {
                            "role": "assistant",
                            "content": "Hello from Eva",
                            "_eva_followup": {
                                "table_spec": {"metric": "mt"},
                            },
                        },
                    ],
                )
                res = client.post(
                    "/chat",
                    headers={"Authorization": "Bearer test-secret-token"},
                    json={
                        "messages": [{"role": "user", "content": "hi"}],
                        "reply_followup": {"table_spec": {"metric": "mt"}},
                    },
                )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True
            assert body["reply"] == "Hello from Eva"
            assert body["messages"][-1]["role"] == "assistant"
            assert body["messages"][-1]["followup"]["table_spec"]["metric"] == "mt"
            kwargs = mock_chat.call_args.kwargs
            assert kwargs.get("forced_prior_spec") == {"metric": "mt"}
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("EVA_BRIDGE_SECRET", None)
