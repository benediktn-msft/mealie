"""
Mealie AI Companion — LLM-powered recipe features via Mealie API + Copilot.

Endpoints (all under /api/ai/):
    POST /chat              Recipe chat (Q&A about a specific recipe)
    POST /adapt             Dietary adaptation (vegan/GF/etc.) — saves as new recipe
    POST /suggest           Smart suggestions from ingredients (legacy alias)
    POST /fridge            Fridge-to-recipe (ingredients -> existing + new)
    POST /meal-plan         Weekly meal plan from existing recipes
    POST /scale             Smart recipe scaling with notes
    POST /auto-tag          Single-recipe auto-tagger
    POST /auto-tag-all      Batch auto-tagger
    POST /shopping-list     Shopping list from list of recipe slugs
    POST /similar           Find similar recipes ("but vegetarian/faster")
    POST /improve           Suggest improvements to a recipe
    GET  /health            Liveness + feature list

All AI calls go through the Copilot OpenAI-compatible proxy.
"""

import json
import os
import logging
import re
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mealie-ai")

MEALIE_URL = os.environ.get("MEALIE_URL", "http://mealie:9000")
MEALIE_API_KEY = os.environ.get("MEALIE_API_KEY", "")
COPILOT_PROXY_URL = os.environ.get("COPILOT_PROXY_URL", "http://copilot-proxy:8080")
MODEL = os.environ.get("AI_MODEL", "gpt-4o")
DEFAULT_TIMEOUT = float(os.environ.get("AI_HTTP_TIMEOUT", "120"))

app = FastAPI(title="Mealie AI Companion", version="2.0.0")


# ────────────────────────── helpers ──────────────────────────

def _parse_llm_json(text: str) -> Any:
    """Parse JSON from an LLM response, stripping markdown fences and finding object/array."""
    if not text:
        raise ValueError("empty LLM response")
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, clean, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    continue
        raise


async def _mealie_get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{MEALIE_URL}/api{path}",
            params=params,
            headers={"Authorization": f"Bearer {MEALIE_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()


async def _mealie_post(path: str, data: dict) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{MEALIE_URL}/api{path}",
            json=data,
            headers={
                "Authorization": f"Bearer {MEALIE_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _mealie_patch(path: str, data: dict) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{MEALIE_URL}/api{path}",
            json=data,
            headers={
                "Authorization": f"Bearer {MEALIE_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _llm_chat(system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            f"{COPILOT_PROXY_URL}/v1/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer proxy"},
        )
        if resp.status_code >= 400:
            log.error("LLM proxy %s: %s", resp.status_code, resp.text[:500])
            raise HTTPException(502, f"LLM proxy error {resp.status_code}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, json.JSONDecodeError) as e:
            raise HTTPException(502, f"Bad LLM response: {e}")


def _recipe_to_text(recipe: dict) -> str:
    parts: list[str] = [f"# {recipe.get('name', 'Untitled')}"]
    if recipe.get("description"):
        parts.append(recipe["description"])
    if recipe.get("recipeYield"):
        parts.append(f"Servings: {recipe['recipeYield']}")
    times: list[str] = []
    if recipe.get("prepTime"):
        times.append(f"Prep: {recipe['prepTime']}")
    if recipe.get("performTime"):
        times.append(f"Cook: {recipe['performTime']}")
    if recipe.get("totalTime"):
        times.append(f"Total: {recipe['totalTime']}")
    if times:
        parts.append(" | ".join(times))

    ingredients = recipe.get("recipeIngredient", [])
    if ingredients:
        parts.append("\n## Ingredients")
        for ing in ingredients:
            if isinstance(ing, dict):
                parts.append(f"- {ing.get('display') or ing.get('note') or ing}")
            else:
                parts.append(f"- {ing}")

    instructions = recipe.get("recipeInstructions", [])
    if instructions:
        parts.append("\n## Instructions")
        for i, step in enumerate(instructions, 1):
            if isinstance(step, dict):
                parts.append(f"{i}. {step.get('text') or step}")
            else:
                parts.append(f"{i}. {step}")
    return "\n".join(parts)


async def _recipe_summary_list(per_page: int = 200) -> list[dict]:
    """Fetch recipe summaries (slug, name, description, tags, recipeCategory)."""
    data = await _mealie_get(f"/recipes?perPage={per_page}&page=1")
    return data.get("items", [])


# ────────────────────────── 1. Recipe Chat ──────────────────────────

class ChatRequest(BaseModel):
    recipe_slug: str
    question: str

class ChatResponse(BaseModel):
    answer: str

@app.post("/api/ai/chat", response_model=ChatResponse)
async def recipe_chat(req: ChatRequest) -> ChatResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")
    system = (
        "You are a helpful cooking assistant. Answer questions about the given recipe. "
        "Be concise, practical, specific. For substitutions consider availability in "
        "Austrian/European supermarkets. Respond in the same language as the question."
    )
    answer = await _llm_chat(system, f"Recipe:\n{_recipe_to_text(recipe)}\n\nQuestion: {req.question}")
    return ChatResponse(answer=answer)


# ────────────────────────── 2. Dietary Adaptation ──────────────────────────

class AdaptRequest(BaseModel):
    recipe_slug: str
    diet: str
    save_as_new: bool = True

class AdaptResponse(BaseModel):
    adapted_recipe: dict
    changes_summary: str
    new_slug: str | None = None

@app.post("/api/ai/adapt", response_model=AdaptResponse)
async def adapt_recipe(req: AdaptRequest) -> AdaptResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")

    system = (
        f"You are a professional chef specializing in dietary adaptations. "
        f"Adapt the recipe to be {req.diet}. Return JSON: "
        '{"name": str, "ingredients": [str], "instructions": [str], "changes": str}. '
        "Use metric units, German language. Use ingredients available in Austrian "
        "supermarkets. Respond ONLY with valid JSON."
    )
    raw = await _llm_chat(system, f"Adapt this recipe:\n{_recipe_to_text(recipe)}", json_mode=True)
    try:
        adapted = _parse_llm_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Failed to parse LLM response: {e}")

    result = {
        "name": adapted.get("name") or f"{recipe['name']} ({req.diet})",
        "ingredients": adapted.get("ingredients", []),
        "instructions": adapted.get("instructions", []),
    }

    new_slug: str | None = None
    if req.save_as_new:
        try:
            new_slug = await _mealie_post("/recipes/create", {"name": result["name"]})
            update_data = {
                "recipeIngredient": [{"note": ing} for ing in result["ingredients"]],
                "recipeInstructions": [{"text": step} for step in result["instructions"]],
                "description": f"AI-adapted from: {recipe['name']} → {req.diet}",
                "tags": (recipe.get("tags") or []) + [{"name": req.diet}],
            }
            await _mealie_patch(f"/recipes/{new_slug}", update_data)
        except Exception as e:
            log.error("Failed to save adapted recipe: %s", e)

    return AdaptResponse(
        adapted_recipe=result,
        changes_summary=adapted.get("changes", ""),
        new_slug=new_slug,
    )


# ────────────────────────── 3. Fridge-to-Recipe (smart suggestions v2) ──────────────────────────

class FridgeRequest(BaseModel):
    ingredients: list[str]
    preferences: str = ""
    max_suggestions: int = 5

class FridgeMatch(BaseModel):
    slug: str
    name: str
    matched_ingredients: list[str]
    missing_ingredients: list[str]
    score: float

class FridgeNew(BaseModel):
    name: str
    description: str
    ingredients: list[str]
    missing_ingredients: list[str]
    difficulty: str = "medium"
    time_minutes: int = 30

class FridgeResponse(BaseModel):
    matches_from_collection: list[FridgeMatch]
    new_ideas: list[FridgeNew]

@app.post("/api/ai/fridge", response_model=FridgeResponse)
async def fridge_to_recipe(req: FridgeRequest) -> FridgeResponse:
    """Match available ingredients against existing collection AND propose new recipes."""
    items = await _recipe_summary_list(per_page=300)

    # Pull full ingredient lists for top-N candidates by name overlap (cheap heuristic).
    have = {i.lower().strip() for i in req.ingredients}
    candidates: list[tuple[float, dict]] = []
    for item in items:
        hay = (item.get("name", "") + " " + (item.get("description") or "")).lower()
        score = sum(1 for ing in have if ing in hay)
        if score:
            candidates.append((score, item))
    candidates.sort(key=lambda t: t[0], reverse=True)

    matches: list[FridgeMatch] = []
    for score_hint, summary in candidates[:8]:
        try:
            full = await _mealie_get(f"/recipes/{summary['slug']}")
        except Exception:
            continue
        ing_texts = []
        for ing in full.get("recipeIngredient") or []:
            if isinstance(ing, dict):
                ing_texts.append((ing.get("display") or ing.get("note") or "").lower())
            else:
                ing_texts.append(str(ing).lower())
        matched = [h for h in have if any(h in t for t in ing_texts)]
        missing = [t for t in ing_texts if not any(h in t for h in have)][:8]
        if matched:
            matches.append(FridgeMatch(
                slug=summary["slug"],
                name=summary["name"],
                matched_ingredients=matched,
                missing_ingredients=missing,
                score=len(matched) / max(1, len(ing_texts)),
            ))
    matches.sort(key=lambda m: m.score, reverse=True)
    matches = matches[:req.max_suggestions]

    # Ask LLM for fresh ideas
    system = (
        "You are a creative home cook in Vienna, Austria. Suggest recipes from the given "
        "ingredients. Consider what's seasonal. Return STRICT JSON: "
        '{"ideas": [{"name": str, "description": str, "ingredients": [str], '
        '"missing_ingredients": [str], "difficulty": "easy|medium|hard", '
        '"time_minutes": int}]}. Use German, metric units.'
    )
    user_msg = (
        f"Available: {', '.join(req.ingredients)}\n"
        f"Preferences: {req.preferences or '(none)'}\n"
        f"Suggest {req.max_suggestions} ideas."
    )
    raw = await _llm_chat(system, user_msg, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
        ideas_raw = parsed.get("ideas") if isinstance(parsed, dict) else parsed
    except Exception as e:
        log.warning("Fridge: failed to parse ideas: %s", e)
        ideas_raw = []

    new_ideas: list[FridgeNew] = []
    for idea in (ideas_raw or [])[:req.max_suggestions]:
        try:
            new_ideas.append(FridgeNew(**{
                "name": idea.get("name", "Idee"),
                "description": idea.get("description", ""),
                "ingredients": idea.get("ingredients", []),
                "missing_ingredients": idea.get("missing_ingredients", []),
                "difficulty": idea.get("difficulty", "medium"),
                "time_minutes": int(idea.get("time_minutes", 30) or 30),
            }))
        except Exception:
            continue

    return FridgeResponse(matches_from_collection=matches, new_ideas=new_ideas)


# Legacy /suggest endpoint preserved for backwards compatibility.

class SuggestRequest(BaseModel):
    ingredients: list[str]
    preferences: str = ""
    max_suggestions: int = 5

@app.post("/api/ai/suggest")
async def suggest_recipes(req: SuggestRequest) -> dict:
    fridge = await fridge_to_recipe(FridgeRequest(**req.model_dump()))
    return {
        "suggestions": [
            {**i.model_dump(), "matches_from_collection": False} for i in fridge.new_ideas
        ] + [
            {
                "name": m.name,
                "description": "",
                "missing_ingredients": m.missing_ingredients,
                "difficulty": "",
                "time_minutes": 0,
                "matches_from_collection": True,
                "slug": m.slug,
            }
            for m in fridge.matches_from_collection
        ],
    }


# ────────────────────────── 4. Meal Planner ──────────────────────────

class MealPlanRequest(BaseModel):
    days: int = 7
    constraints: str = ""  # e.g. "vegetarian, max 30 minutes on weeknights"
    include_breakfast: bool = False
    include_lunch: bool = True
    include_dinner: bool = True

class MealPlanDay(BaseModel):
    day: str
    breakfast: dict | None = None
    lunch: dict | None = None
    dinner: dict | None = None
    notes: str = ""

class MealPlanResponse(BaseModel):
    plan: list[MealPlanDay]
    rationale: str

@app.post("/api/ai/meal-plan", response_model=MealPlanResponse)
async def meal_plan(req: MealPlanRequest) -> MealPlanResponse:
    items = await _recipe_summary_list(per_page=300)
    catalogue = "\n".join(f"- {i['slug']}: {i['name']}" for i in items[:200])

    meals = [m for m, on in (("breakfast", req.include_breakfast), ("lunch", req.include_lunch), ("dinner", req.include_dinner)) if on]
    system = (
        "You are a meal planner. Build a balanced weekly plan using ONLY recipes from "
        "the user's catalogue (referenced by slug). Aim for variety, balance protein/veg/"
        "carbs, avoid repeats, batch leftovers. Return STRICT JSON: "
        '{"plan": [{"day": "Mo|Di|...", "breakfast": {"slug": str, "name": str} | null, '
        '"lunch": {"slug": str, "name": str} | null, "dinner": {"slug": str, "name": str} | null, '
        '"notes": str}], "rationale": str}.'
    )
    user_msg = (
        f"Days: {req.days}\nMeals to plan: {', '.join(meals)}\n"
        f"Constraints: {req.constraints or '(none)'}\n\n"
        f"Recipe catalogue (slug: name):\n{catalogue}"
    )
    raw = await _llm_chat(system, user_msg, max_tokens=3000, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Failed to parse meal plan: {e}")

    plan_days = []
    for d in parsed.get("plan", []):
        plan_days.append(MealPlanDay(
            day=d.get("day", "?"),
            breakfast=d.get("breakfast"),
            lunch=d.get("lunch"),
            dinner=d.get("dinner"),
            notes=d.get("notes", ""),
        ))
    return MealPlanResponse(plan=plan_days, rationale=parsed.get("rationale", ""))


# ────────────────────────── 5. Recipe Scaling ──────────────────────────

class ScaleRequest(BaseModel):
    recipe_slug: str
    target_servings: float = Field(..., gt=0)

class ScaleResponse(BaseModel):
    original_servings: float | str | None
    target_servings: float
    scaled_ingredients: list[str]
    notes: str

@app.post("/api/ai/scale", response_model=ScaleResponse)
async def scale_recipe(req: ScaleRequest) -> ScaleResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")

    system = (
        "You are a chef who scales recipes intelligently. Don't just multiply blindly: "
        "round egg counts sensibly, warn when oven/pan size matters, note when timing "
        "or texture changes (e.g. doubling baked goods often needs longer time, not "
        "double). Return STRICT JSON: "
        '{"scaled_ingredients": [str], "notes": str}. German, metric units.'
    )
    user_msg = (
        f"Original recipe ({recipe.get('recipeYield', 'unbekannt')} servings):\n"
        f"{_recipe_to_text(recipe)}\n\nScale to {req.target_servings} servings."
    )
    raw = await _llm_chat(system, user_msg, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Failed to parse scaling response: {e}")

    return ScaleResponse(
        original_servings=recipe.get("recipeYield"),
        target_servings=req.target_servings,
        scaled_ingredients=parsed.get("scaled_ingredients", []),
        notes=parsed.get("notes", ""),
    )


# ────────────────────────── 6. Auto-Tagger ──────────────────────────

class AutoTagRequest(BaseModel):
    recipe_slug: str
    apply: bool = True

class AutoTagResponse(BaseModel):
    suggested_tags: list[str]
    applied: bool

async def _ensure_tag(client: httpx.AsyncClient, name: str) -> dict | None:
    """Find or create a tag in Mealie organizer API. Returns the tag object or None."""
    try:
        search = await client.get(
            f"{MEALIE_URL}/api/organizers/tags",
            headers={"Authorization": f"Bearer {MEALIE_API_KEY}"},
            params={"perPage": 200, "search": name},
        )
        if search.status_code == 200:
            for tag in search.json().get("items", []):
                if tag.get("name", "").lower() == name.lower():
                    return tag
    except Exception:
        pass
    try:
        resp = await client.post(
            f"{MEALIE_URL}/api/organizers/tags",
            json={"name": name},
            headers={"Authorization": f"Bearer {MEALIE_API_KEY}", "Content-Type": "application/json"},
        )
        if resp.status_code in (200, 201):
            return resp.json()
    except Exception as e:
        log.warning("create tag '%s' failed: %s", name, e)
    return None


@app.post("/api/ai/auto-tag", response_model=AutoTagResponse)
async def auto_tag_recipe(req: AutoTagRequest) -> AutoTagResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")

    system = (
        "Analyze this recipe and suggest tags. Return STRICT JSON: "
        '{"tags": [str]}. Use these categories: '
        "Cuisine (österreichisch, italienisch, asiatisch, mexikanisch, indisch, ...); "
        "Meal type (frühstück, mittagessen, abendessen, snack, dessert, beilage); "
        "Season (frühling, sommer, herbst, winter, ganzjährig); "
        "Difficulty (einfach, mittel, aufwändig); "
        "Diet (vegetarisch, vegan, glutenfrei, low-carb) only if applicable; "
        "Time (schnell, unter-1-stunde, meal-prep); "
        "Style (comfort-food, gesund, festlich, alltag). "
        "Only include tags that genuinely apply. 5-8 tags max."
    )
    raw = await _llm_chat(system, f"Analyze:\n{_recipe_to_text(recipe)}", json_mode=True)
    try:
        result = _parse_llm_json(raw)
        tags = result.get("tags", []) if isinstance(result, dict) else []
    except Exception as e:
        log.error("Failed to parse auto-tag response: %s", e)
        tags = []

    applied = False
    if req.apply and tags:
        try:
            existing = list(recipe.get("tags") or [])
            existing_names = {t.get("name", "").lower() for t in existing}
            async with httpx.AsyncClient(timeout=30) as client:
                for name in tags:
                    if name.lower() in existing_names:
                        continue
                    tag_obj = await _ensure_tag(client, name)
                    if tag_obj:
                        existing.append(tag_obj)
                        existing_names.add(name.lower())
            await _mealie_patch(f"/recipes/{req.recipe_slug}", {"tags": existing})
            applied = True
        except Exception as e:
            log.error("Failed to apply tags: %s", e)

    return AutoTagResponse(suggested_tags=tags, applied=applied)


class BatchTagRequest(BaseModel):
    max_recipes: int = 50

class BatchTagResponse(BaseModel):
    tagged: int
    skipped: int
    errors: int

@app.post("/api/ai/auto-tag-all", response_model=BatchTagResponse)
async def batch_auto_tag(req: BatchTagRequest) -> BatchTagResponse:
    items = await _recipe_summary_list(per_page=req.max_recipes)
    tagged = skipped = errors = 0
    for summary in items:
        if len(summary.get("tags") or []) >= 3:
            skipped += 1
            continue
        try:
            r = await auto_tag_recipe(AutoTagRequest(recipe_slug=summary["slug"], apply=True))
            if r.applied:
                tagged += 1
            else:
                skipped += 1
        except Exception as e:
            log.error("Error tagging %s: %s", summary.get("slug"), e)
            errors += 1
    return BatchTagResponse(tagged=tagged, skipped=skipped, errors=errors)


# ────────────────────────── 7. Shopping List ──────────────────────────

class ShoppingListRequest(BaseModel):
    recipe_slugs: list[str]
    servings_overrides: dict[str, float] = Field(default_factory=dict)
    pantry: list[str] = Field(default_factory=list)  # things you already have

class ShoppingItem(BaseModel):
    item: str
    quantity: str
    section: str  # "Obst & Gemüse", "Milchprodukte", "Trocken", ...
    from_recipes: list[str]

class ShoppingListResponse(BaseModel):
    sections: dict[str, list[ShoppingItem]]
    flat: list[ShoppingItem]

@app.post("/api/ai/shopping-list", response_model=ShoppingListResponse)
async def shopping_list(req: ShoppingListRequest) -> ShoppingListResponse:
    recipe_blobs: list[dict] = []
    for slug in req.recipe_slugs:
        try:
            r = await _mealie_get(f"/recipes/{slug}")
            recipe_blobs.append({
                "slug": slug,
                "name": r.get("name", slug),
                "servings": r.get("recipeYield"),
                "target_servings": req.servings_overrides.get(slug),
                "ingredients": [
                    (ing.get("display") or ing.get("note") or "") if isinstance(ing, dict) else str(ing)
                    for ing in (r.get("recipeIngredient") or [])
                ],
            })
        except Exception as e:
            log.warning("shopping-list: skip %s: %s", slug, e)

    if not recipe_blobs:
        raise HTTPException(400, "No valid recipes provided")

    system = (
        "You are a meal-prep assistant. Build a consolidated shopping list from these "
        "recipes (consolidate duplicate ingredients, sum quantities, scale by "
        "target_servings/servings if given). Group by Austrian-supermarket section. "
        "Skip items in the user's pantry. Return STRICT JSON: "
        '{"items": [{"item": str, "quantity": str, "section": str, '
        '"from_recipes": [str]}]}. German, metric.'
    )
    user_msg = (
        f"Pantry (skip these): {', '.join(req.pantry) or '(empty)'}\n\n"
        f"Recipes:\n{json.dumps(recipe_blobs, ensure_ascii=False, indent=2)}"
    )
    raw = await _llm_chat(system, user_msg, max_tokens=3000, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
        raw_items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    except Exception as e:
        raise HTTPException(500, f"Failed to parse shopping list: {e}")

    flat = [ShoppingItem(
        item=i.get("item", ""),
        quantity=i.get("quantity", ""),
        section=i.get("section", "Sonstiges"),
        from_recipes=i.get("from_recipes", []),
    ) for i in raw_items]

    sections: dict[str, list[ShoppingItem]] = {}
    for it in flat:
        sections.setdefault(it.section, []).append(it)
    return ShoppingListResponse(sections=sections, flat=flat)


# ────────────────────────── 8. Similar Recipes ──────────────────────────

class SimilarRequest(BaseModel):
    recipe_slug: str
    twist: str = ""  # "but vegetarian", "but quicker", "but spicier"
    max_results: int = 5

class SimilarMatch(BaseModel):
    slug: str
    name: str
    why: str

class SimilarResponse(BaseModel):
    from_collection: list[SimilarMatch]
    new_idea: dict

@app.post("/api/ai/similar", response_model=SimilarResponse)
async def similar_recipes(req: SimilarRequest) -> SimilarResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")

    items = await _recipe_summary_list(per_page=300)
    catalogue = "\n".join(f"- {i['slug']}: {i['name']}" for i in items[:200])

    system = (
        "You match a target recipe against a catalogue, optionally with a twist (e.g. "
        "'but vegetarian'). Pick recipes that share spirit/technique/cuisine. Also "
        "propose ONE new idea outside the catalogue. Return STRICT JSON: "
        '{"matches": [{"slug": str, "name": str, "why": str}], '
        '"new_idea": {"name": str, "description": str, "ingredients": [str]}}. German.'
    )
    user_msg = (
        f"Target recipe:\n{_recipe_to_text(recipe)}\n\n"
        f"Twist: {req.twist or '(no twist — pure similarity)'}\n\n"
        f"Catalogue (slug: name):\n{catalogue}\n\n"
        f"Pick up to {req.max_results} matches."
    )
    raw = await _llm_chat(system, user_msg, max_tokens=2000, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Failed to parse similar response: {e}")

    catalogue_slugs = {i["slug"] for i in items}
    matches: list[SimilarMatch] = []
    for m in parsed.get("matches", [])[:req.max_results]:
        slug = m.get("slug")
        if slug in catalogue_slugs:
            matches.append(SimilarMatch(slug=slug, name=m.get("name", ""), why=m.get("why", "")))
    return SimilarResponse(from_collection=matches, new_idea=parsed.get("new_idea") or {})


# ────────────────────────── 9. Recipe Improvement ──────────────────────────

class ImproveRequest(BaseModel):
    recipe_slug: str
    focus: str = ""  # e.g. "more flavor", "healthier", "weeknight-friendly"

class ImproveSuggestion(BaseModel):
    type: str  # "ingredient", "technique", "timing", "presentation"
    suggestion: str
    why: str
    impact: str  # "low" | "medium" | "high"

class ImproveResponse(BaseModel):
    suggestions: list[ImproveSuggestion]
    one_line_summary: str

@app.post("/api/ai/improve", response_model=ImproveResponse)
async def improve_recipe(req: ImproveRequest) -> ImproveResponse:
    try:
        recipe = await _mealie_get(f"/recipes/{req.recipe_slug}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Recipe not found: {e}")

    system = (
        "You are a Michelin-trained chef reviewing a home cook's recipe. Suggest "
        "concrete, actionable improvements (no fluff). Return STRICT JSON: "
        '{"suggestions": [{"type": "ingredient|technique|timing|presentation", '
        '"suggestion": str, "why": str, "impact": "low|medium|high"}], '
        '"one_line_summary": str}. 4-7 suggestions. German.'
    )
    user_msg = f"Recipe:\n{_recipe_to_text(recipe)}\n\nFocus: {req.focus or '(general improvement)'}"
    raw = await _llm_chat(system, user_msg, json_mode=True)
    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Failed to parse improvement response: {e}")

    suggestions = [
        ImproveSuggestion(
            type=s.get("type", "technique"),
            suggestion=s.get("suggestion", ""),
            why=s.get("why", ""),
            impact=s.get("impact", "medium"),
        )
        for s in parsed.get("suggestions", [])
    ]
    return ImproveResponse(suggestions=suggestions, one_line_summary=parsed.get("one_line_summary", ""))


# ────────────────────────── health ──────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "model": MODEL,
        "features": [
            "chat",
            "adapt",
            "fridge",
            "suggest",  # legacy alias for fridge
            "meal-plan",
            "scale",
            "auto-tag",
            "auto-tag-all",
            "shopping-list",
            "similar",
            "improve",
        ],
    }
