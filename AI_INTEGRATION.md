# Mealie AI Integration

This is **benediktn-msft's fork** of [mealie-recipes/mealie](https://github.com/mealie-recipes/mealie).

The only modification vs. upstream is a small **Copilot proxy** sidecar that lets
Mealie's built-in AI features run on **GitHub Copilot** instead of a direct OpenAI API key.
The Mealie codebase itself is left untouched, so upstream syncs stay clean.

```
                    ┌──────────────────────────────┐
   browser ─────►   │ mealie  (upstream, unchanged)│
                    │  9925:9000                   │
                    │  • image-to-recipe           │
                    │  • video-to-recipe           │
                    │  • URL-to-recipe             │
                    │  • OCR / transcription       │
                    └─────────────┬────────────────┘
                                  │ OPENAI_BASE_URL=http://copilot-proxy:8080/v1
                                  ▼
                    ┌──────────────────────────────┐
                    │ copilot-proxy                │
                    │  • OpenAI-compatible REST    │
                    │  • Forwards to Copilot API   │
                    │  • Falls back to OpenAI key  │
                    │    only if Copilot token bad │
                    └──────────────────────────────┘
```

## One frontend, one app

Mealie's own Vue/Nuxt UI is the **only** UI. There is no second app, no second
port, no extra Telegram-style sidecar. Every AI feature shown to the user is a
feature Mealie itself ships and renders.

> **Removed 2026-04-29:** the earlier `mealie-ai` FastAPI sidecar on port 9926
> (8 endpoints: chat, fridge, meal-plan, scale, auto-tag, shopping-list,
> similar, improve) has been deleted. Cleanly integrating those into Mealie's
> Vue frontend would have required a custom Mealie image build plus weekly
> merge conflicts on actively-developed components (`RecipeContextMenu.vue`,
> dashboard, admin, meal-planner, shopping-list). Most features also duplicated
> capabilities Mealie already has. Not worth the maintenance burden for a
> single-user instance.

## Sidecar service

| Service         | Path (in this repo)              | Port (network) | Purpose                              |
| --------------- | -------------------------------- | -------------- | ------------------------------------ |
| `copilot-proxy` | `ai-sidecar/copilot-proxy/`      | 8080 internal  | OpenAI-compat proxy → GitHub Copilot |

Not published on the host — `mealie` reaches it via the docker network.

## Mealie environment

```
OPENAI_API_KEY=copilot-proxy            # placeholder, ignored by proxy
OPENAI_BASE_URL=http://copilot-proxy:8080/v1
OPENAI_MODEL=gpt-4o
OPENAI_ENABLE_IMAGE_SERVICES=true
OPENAI_ENABLE_TRANSCRIPTION_SERVICES=true
```

## How the proxy works

`copilot-proxy` reads a Copilot session token from
`/home/nuc/docker/mealie/copilot-token.json` (mounted read-only) and forwards
OpenAI-compatible chat-completion requests to
`https://api.enterprise.githubcopilot.com`. It patches JSON-schema
`response_format` objects (Copilot needs `additionalProperties: false`) and
falls back to a real OpenAI key only if the Copilot token is missing/expired.

⚠️ The Mealie container itself never sees a real OpenAI key. `OPENAI_BASE_URL`
points at the proxy.

## Upstream sync

[`.github/workflows/sync-upstream.yml`](.github/workflows/sync-upstream.yml) runs weekly
(Mon 04:17 UTC) and on demand. It tries fast-forward / clean merge from
`mealie-recipes/mealie@mealie-next` into our `mealie-next` branch. On conflicts
it opens a PR on a `sync/upstream-<timestamp>` branch for manual review.
Because nothing in `mealie/` or `frontend/` is patched, sync should rarely
conflict.

## Custom branch

Day-to-day work happens on `custom-fork`. `mealie-next` mirrors upstream and is
kept in sync via the workflow above.
