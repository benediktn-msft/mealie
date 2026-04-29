# `ai-sidecar/` — Copilot proxy for Mealie

This directory holds the OpenAI-compatible proxy that lets Mealie's **built-in** AI
features (image-to-recipe, video-to-recipe, URL-to-recipe, OCR, transcription, …)
run on **GitHub Copilot** instead of a paid OpenAI API key.

The Mealie codebase itself is not modified — upstream sync stays clean.

> **History:** an earlier iteration also shipped a separate `mealie-ai` FastAPI
> sidecar on port 9926 with 8 extra endpoints (chat, fridge, meal-plan, scale,
> auto-tag, shopping-list, similar, improve). It was removed on 2026-04-29:
> Benni wants **one** frontend, and integrating those features cleanly into
> Mealie's Vue UI would have required a custom Mealie image build plus
> persistent merge conflicts on every weekly upstream sync — too much
> maintenance burden for features that mostly duplicate what Mealie already
> does. Use Mealie's built-in AI features instead; they go through this proxy.

## Layout

```
ai-sidecar/
├── copilot-proxy/       # OpenAI-compat proxy → GitHub Copilot
│   ├── proxy.py
│   └── Dockerfile
└── deploy/
    └── docker-compose.yml   # NUC stack: mealie + copilot-proxy
```

## Build & run on the NUC

```bash
cd /home/nuc/docker/mealie
docker build -t copilot-proxy:latest /path/to/mealie-fork/ai-sidecar/copilot-proxy
docker compose up -d
```

The proxy listens on port `8080` **inside the docker network only** — it is not
exposed to the host. Mealie reaches it via `http://copilot-proxy:8080/v1`.

## What Mealie sees

```
OPENAI_API_KEY=copilot-proxy        # placeholder, the proxy ignores it
OPENAI_BASE_URL=http://copilot-proxy:8080/v1
OPENAI_MODEL=gpt-4o
OPENAI_ENABLE_IMAGE_SERVICES=true
OPENAI_ENABLE_TRANSCRIPTION_SERVICES=true
```

## How the proxy works

* Reads a Copilot session token from `/data/copilot-token.json` (mounted
  read-only from `/home/nuc/docker/mealie/copilot-token.json`).
* Forwards OpenAI-compatible chat-completion requests to
  `https://api.enterprise.githubcopilot.com`.
* Patches JSON-schema `response_format` objects (Copilot requires
  `additionalProperties: false`).
* Falls back to a real OpenAI key (`OPENAI_FALLBACK_KEY`) only if the Copilot
  token is missing or expired. Leave the var empty to fail closed.

See [`AI_INTEGRATION.md`](../AI_INTEGRATION.md) for the architecture diagram.
