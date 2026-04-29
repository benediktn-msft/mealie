# Mealie AI Integration

This is **benediktn-msft's fork** of [mealie-recipes/mealie](https://github.com/mealie-recipes/mealie),
extended with an AI companion sidecar that uses **GitHub Copilot** (via an OpenAI-compatible proxy)
as the LLM backend — never a direct OpenAI API key.

The Mealie codebase itself is left untouched so upstream syncs stay clean. All AI features live in
two sidecar services and an optional docker-compose overlay.

```
                    ┌──────────────────────────────┐
   browser ─────►   │ mealie  (upstream, unchanged)│
                    │  9925:9000                   │
                    └─────────────┬────────────────┘
                                  │ OPENAI_BASE_URL=http://copilot-proxy:8080/v1
                                  ▼
                    ┌──────────────────────────────┐
                    │ copilot-proxy                │
                    │  • OpenAI-compatible REST    │
                    │  • Forwards to Copilot API   │
                    │  • Falls back to OpenAI key  │
                    └─────────────┬────────────────┘
                                  ▲
                                  │ Bearer proxy
                                  │
                    ┌──────────────────────────────┐
   browser/CLI ──►  │ mealie-ai (sidecar)          │
                    │  9926:8081                   │
                    │  /api/ai/{chat,fridge,...}   │
                    └──────────────────────────────┘
```

## Sidecar services

The two services are kept in sibling repos / workspace folders:

| Service           | Repo path (workspace)                       | Port | Purpose                                |
| ----------------- | ------------------------------------------- | ---- | -------------------------------------- |
| `mealie-ai`       | `~/.openclaw/workspace/mealie-ai/`          | 8081 | FastAPI: 8 user-facing AI features     |
| `copilot-proxy`   | `~/.openclaw/workspace/mealie-copilot-proxy/` | 8080 | OpenAI-compat proxy → GitHub Copilot |

Both have small Dockerfiles (Python 3.12-slim) and run on the NUC under
`/home/nuc/docker/mealie/docker-compose.yml`.

## AI endpoints (mealie-ai, port 9926 on host)

All return JSON. All speak German by default and use metric units.

| Method | Path                      | What it does                                                                |
| ------ | ------------------------- | --------------------------------------------------------------------------- |
| POST   | `/api/ai/chat`            | Q&A about a single recipe (substitutions, technique, etc.)                  |
| POST   | `/api/ai/adapt`           | Convert recipe to a diet (vegan/GF/low-carb…) — saves as new recipe        |
| POST   | `/api/ai/fridge`          | Ingredients in → matching existing recipes + new ideas                      |
| POST   | `/api/ai/suggest`         | Legacy alias for `/fridge` (kept for backwards compat)                      |
| POST   | `/api/ai/meal-plan`       | Build a balanced weekly plan from existing collection                       |
| POST   | `/api/ai/scale`           | Smart scaling (rounds eggs, warns on bake timing, pan-size notes)           |
| POST   | `/api/ai/auto-tag`        | Suggest + apply tags (cuisine, season, difficulty, diet, time, style)       |
| POST   | `/api/ai/auto-tag-all`    | Batch auto-tag all under-tagged recipes                                     |
| POST   | `/api/ai/shopping-list`   | Consolidated, sectioned shopping list from a list of recipe slugs           |
| POST   | `/api/ai/similar`         | "Find me something like this, but vegetarian/quicker"                       |
| POST   | `/api/ai/improve`         | Michelin-style suggestions to improve an existing recipe                    |
| GET    | `/health`                 | Liveness + feature list                                                     |

Full request/response schemas are defined as Pydantic models in
[`mealie-ai/app.py`](https://github.com/benediktn-msft/mealie-ai-companion).

## AI backend — Copilot, not OpenAI

`copilot-proxy` reads a Copilot session token from
`/home/nuc/docker/mealie/copilot-token.json` (mounted read-only into the container) and forwards
OpenAI-compatible chat-completion requests to `https://api.enterprise.githubcopilot.com`. It also
patches JSON-schema response_format objects (Copilot needs `additionalProperties: false`) and falls
back to a real OpenAI key only if the Copilot token is missing/expired.

⚠️ The Mealie container itself never sees a real OpenAI key. `OPENAI_BASE_URL` points at the proxy.

## Upstream sync

[`.github/workflows/sync-upstream.yml`](.github/workflows/sync-upstream.yml) runs weekly (Mon 04:17 UTC)
and on demand. It tries fast-forward / clean merge from `mealie-recipes/mealie@mealie-next` into our
`mealie-next` branch. On conflicts it opens a PR on a `sync/upstream-<timestamp>` branch for manual
review.

## Custom branch

Day-to-day work happens on `custom-fork`. `mealie-next` mirrors upstream and is kept in sync via the
workflow above.
