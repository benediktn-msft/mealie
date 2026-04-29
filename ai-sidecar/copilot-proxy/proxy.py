"""
Copilot-to-OpenAI proxy for Mealie.

Reads a Copilot token from a mounted JSON file, auto-refreshes on 401,
and proxies OpenAI-compatible requests to the GitHub Copilot API.

Mealie points OPENAI_BASE_URL at this proxy.
"""

import json
import os
import time
import threading
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("copilot-proxy")

COPILOT_TOKEN_FILE = os.environ.get("COPILOT_TOKEN_FILE", "/data/copilot-token.json")
COPILOT_API_BASE = "https://api.enterprise.githubcopilot.com"
COPILOT_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.95.0",
}

# Fallback to real OpenAI if configured
OPENAI_FALLBACK_KEY = os.environ.get("OPENAI_FALLBACK_KEY", "")
OPENAI_FALLBACK_BASE = "https://api.openai.com/v1"

app = FastAPI(title="Copilot Proxy for Mealie")

_token_cache = {"token": None, "mtime": 0}
_lock = threading.Lock()


def _read_token() -> str | None:
    """Read token from file, with mtime caching."""
    path = Path(COPILOT_TOKEN_FILE)
    if not path.exists():
        logger.warning(f"Token file not found: {path}")
        return None

    mtime = path.stat().st_mtime
    with _lock:
        if mtime != _token_cache["mtime"]:
            try:
                data = json.loads(path.read_text())
                _token_cache["token"] = data.get("token") or data.get("access_token")
                _token_cache["mtime"] = mtime
                logger.info("Refreshed Copilot token from file")
            except Exception as e:
                logger.error(f"Failed to read token: {e}")
                return _token_cache["token"]
        return _token_cache["token"]


def _get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        **COPILOT_HEADERS,
    }


@app.get("/v1/models")
async def list_models():
    """Fake models endpoint so Mealie doesn't complain."""
    return {
        "object": "list",
        "data": [
            {"id": "gpt-4o", "object": "model", "owned_by": "copilot"},
            {"id": "gpt-4o-mini", "object": "model", "owned_by": "copilot"},
            {"id": "claude-sonnet-4", "object": "model", "owned_by": "copilot"},
        ],
    }


def _fix_schema(obj):
    """Recursively add additionalProperties:false to JSON schema objects for Copilot compatibility."""
    if isinstance(obj, dict):
        if obj.get("type") == "object" and "properties" in obj:
            obj.setdefault("additionalProperties", False)
        for v in obj.values():
            _fix_schema(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_schema(item)
    return obj


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    """Proxy all /v1/* requests to Copilot API."""

    token = _read_token()
    if not token:
        if OPENAI_FALLBACK_KEY:
            logger.warning("No Copilot token, falling back to OpenAI")
            return await _forward_to_openai(path, request)
        return Response(content='{"error": "No Copilot token available"}', status_code=503)

    body = await request.body()

    # Fix structured output schemas for Copilot compatibility
    if body and path == "chat/completions":
        try:
            data = json.loads(body)
            if "response_format" in data:
                _fix_schema(data["response_format"])
                body = json.dumps(data).encode()
        except (json.JSONDecodeError, Exception):
            pass

    # Try Copilot
    async with httpx.AsyncClient(timeout=300) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=f"{COPILOT_API_BASE}/{path}",
                content=body,
                headers=_get_headers(token),
            )

            if resp.status_code == 401 and OPENAI_FALLBACK_KEY:
                logger.warning("Copilot 401, falling back to OpenAI")
                return await _forward_to_openai(path, request, body)

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={"content-type": resp.headers.get("content-type", "application/json")},
            )
        except Exception as e:
            logger.error(f"Copilot request failed: {e}")
            if OPENAI_FALLBACK_KEY:
                return await _forward_to_openai(path, request, body)
            return Response(content=f'{{"error": "{str(e)}"}}', status_code=502)


async def _forward_to_openai(path: str, request: Request, body: bytes | None = None):
    """Fallback to OpenAI API."""
    if body is None:
        body = await request.body()

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.request(
            method=request.method,
            url=f"{OPENAI_FALLBACK_BASE}/{path}",
            content=body,
            headers={
                "Authorization": f"Bearer {OPENAI_FALLBACK_KEY}",
                "Content-Type": "application/json",
            },
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
        )


@app.get("/health")
async def health():
    token = _read_token()
    return {
        "status": "ok",
        "copilot_token_available": token is not None,
        "fallback_configured": bool(OPENAI_FALLBACK_KEY),
    }
