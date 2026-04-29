# `ai-sidecar/` — Mealie AI companion services

This directory holds everything needed to run the AI features alongside upstream Mealie,
**without modifying upstream code**. Upstream sync (see
[`.github/workflows/sync-upstream.yml`](../.github/workflows/sync-upstream.yml)) stays clean.

## Layout

```
ai-sidecar/
├── mealie-ai/           # FastAPI sidecar — 8 user-facing AI endpoints
│   ├── app.py
│   └── Dockerfile
├── copilot-proxy/       # OpenAI-compat proxy → GitHub Copilot
│   ├── proxy.py
│   └── Dockerfile
└── deploy/
    └── docker-compose.yml   # NUC stack: mealie + copilot-proxy + mealie-ai
```

## Build & run on the NUC

```bash
cd /home/nuc/docker/mealie
# build sidecars
docker build -t copilot-proxy:latest /path/to/mealie/ai-sidecar/copilot-proxy
docker build -t mealie-ai:latest    /path/to/mealie/ai-sidecar/mealie-ai
# bring up
docker compose up -d
```

## Endpoints

The sidecar listens on port `9926` (host). All routes:

```
POST /api/ai/chat            { recipe_slug, question }
POST /api/ai/adapt           { recipe_slug, diet, save_as_new? }
POST /api/ai/fridge          { ingredients[], preferences?, max_suggestions? }
POST /api/ai/suggest         (legacy alias for /fridge)
POST /api/ai/meal-plan       { days?, constraints?, include_breakfast?, ... }
POST /api/ai/scale           { recipe_slug, target_servings }
POST /api/ai/auto-tag        { recipe_slug, apply? }
POST /api/ai/auto-tag-all    { max_recipes? }
POST /api/ai/shopping-list   { recipe_slugs[], servings_overrides?, pantry? }
POST /api/ai/similar         { recipe_slug, twist?, max_results? }
POST /api/ai/improve         { recipe_slug, focus? }
GET  /health
```

See [`AI_INTEGRATION.md`](../AI_INTEGRATION.md) for the architecture diagram and Copilot
backend details.
