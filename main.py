import base64
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from google import genai
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID

load_dotenv()

APP_VERSION = "1.0.6"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WHATFONTIS_API_KEY = os.getenv("WHATFONTIS_API_KEY", "")
FONTDROP_API_SECRET = os.getenv("FONTDROP_API_SECRET", "")
FONTDROP_CLIENT_TOKEN = os.getenv("FONTDROP_CLIENT_TOKEN", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

APPWRITE_ENDPOINT = os.getenv("APPWRITE_ENDPOINT", "")
APPWRITE_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID", "")
APPWRITE_API_KEY = os.getenv("APPWRITE_API_KEY", "")
APPWRITE_DATABASE_ID = os.getenv("APPWRITE_DATABASE_ID", "")
APPWRITE_USAGE_LOG_TABLE_ID = os.getenv("APPWRITE_USAGE_LOG_TABLE_ID", "")

app = FastAPI(title="FontDrop API", version=APP_VERSION)

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

appwrite_db = None
if APPWRITE_ENDPOINT and APPWRITE_PROJECT_ID and APPWRITE_API_KEY:
    try:
        _appwrite_client = Client()
        _appwrite_client.set_endpoint(APPWRITE_ENDPOINT)
        _appwrite_client.set_project(APPWRITE_PROJECT_ID)
        _appwrite_client.set_key(APPWRITE_API_KEY)
        appwrite_db = Databases(_appwrite_client)
        print("✓ Appwrite usage logging configured")
    except Exception as e:
        print(f"⚠ Appwrite init failed: {e}")
        appwrite_db = None


class IdentifyRequest(BaseModel):
    image_base64: str
    device_id: str | None = None
    app_version: str | None = None
    script: str | None = "latin"


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "fontdrop-api",
        "version": "1.0.6",
        "gemini_configured": bool(GEMINI_API_KEY),
        "whatfontis_configured": bool(WHATFONTIS_API_KEY),
        "appwrite_configured": bool(appwrite_db and APPWRITE_DATABASE_ID and APPWRITE_USAGE_LOG_TABLE_ID),
    }


def verify_api_secret(x_fontdrop_secret: str | None):
    allowed_tokens = set()

    if FONTDROP_API_SECRET:
        allowed_tokens.add(FONTDROP_API_SECRET)

    if FONTDROP_CLIENT_TOKEN:
        allowed_tokens.add(FONTDROP_CLIENT_TOKEN)

    # If no token is configured, allow requests.
    # In production, configure at least FONTDROP_CLIENT_TOKEN.
    if not allowed_tokens:
        return

    if x_fontdrop_secret not in allowed_tokens:
        raise HTTPException(status_code=401, detail="Unauthorized")


def load_json_file(filename: str, default):
    path = Path(__file__).parent / filename
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def log_usage_event(
    device_id: str,
    action: str,
    status: str,
    app_version: str | None = None,
    script: str | None = None,
    font_detected: str | None = None,
    error: str | None = None,
    usage: dict | None = None,
):
    """Best-effort write to Appwrite usage_log. Never blocks the main result."""
    if not appwrite_db or not APPWRITE_DATABASE_ID or not APPWRITE_USAGE_LOG_TABLE_ID:
        return

    try:
        metadata = {
            "status": status,
            "font_detected": font_detected,
            "script": script,
            "app_version": app_version,
            "error": error,
            "usage": usage or {},
        }

        appwrite_db.create_document(
            database_id=APPWRITE_DATABASE_ID,
            collection_id=APPWRITE_USAGE_LOG_TABLE_ID,
            document_id=ID.unique(),
            data={
                "user_id": device_id or "anonymous",
                "device_id": device_id or "anonymous",
                "action": action,
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )
    except Exception as e:
        print(f"⚠ Appwrite usage_log write failed: {e}")


FREE_MONTHLY_SCAN_LIMIT = 20
PRO_MONTHLY_SCAN_LIMIT = 250
STUDIO_MONTHLY_SCAN_LIMIT = 1000


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def quota_path() -> Path:
    path = Path(__file__).parent / "data"
    path.mkdir(exist_ok=True)
    return path / "quota_usage.json"


def load_quota_usage() -> dict:
    path = quota_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_quota_usage(data: dict):
    path = quota_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _plan_limit(plan: str) -> int:
    plan = str(plan or "free").lower()

    if plan in ("pro", "basic", "paid"):
        return PRO_MONTHLY_SCAN_LIMIT

    if plan in ("studio", "agency"):
        return STUDIO_MONTHLY_SCAN_LIMIT

    return FREE_MONTHLY_SCAN_LIMIT


def _get_subscription_plan(device_id: str) -> str:
    """Return free/pro/studio according to Appwrite subscriptions."""
    if not device_id:
        return "free"

    try:
        from appwrite.query import Query

        res = appwrite_db.list_documents(
            database_id=APPWRITE_DATABASE_ID,
            collection_id="subscriptions",
            queries=[
                Query.equal("user_id", device_id),
                Query.limit(20),
            ],
        )

        docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
        if not docs:
            return "free"

        active_docs = []

        for sub in docs:
            if not isinstance(sub, dict):
                continue

            status = str(sub.get("status", "")).lower()
            plan = str(sub.get("plan", "free")).lower()

            # Old trial/free rows should behave as free plan.
            if plan in ("trial", ""):
                plan = "free"

            # Free plan is active as long as a row exists.
            if plan == "free":
                active_docs.append({**sub, "plan": "free"})
                continue

            if status not in ("active", "trialing", "on_trial", "past_due"):
                continue

            # Optional expiry check for paid plans.
            expires = sub.get("current_period_end") or sub.get("trial_ends_at")
            if expires:
                try:
                    exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    if exp < datetime.now(timezone.utc):
                        continue
                except Exception:
                    pass

            active_docs.append({**sub, "plan": plan})

        if not active_docs:
            return "free"

        # Prefer strongest plan if multiple rows exist.
        priority = {
            "studio": 3,
            "pro": 2,
            "basic": 2,
            "paid": 2,
            "free": 1,
            "trial": 1,
        }

        best = sorted(
            active_docs,
            key=lambda x: priority.get(str(x.get("plan", "free")).lower(), 1),
            reverse=True,
        )[0]

        plan = str(best.get("plan", "free")).lower()

        if plan in ("basic", "paid"):
            return "pro"

        if plan in ("trial", ""):
            return "free"

        if plan not in ("free", "pro", "studio"):
            return "free"

        return plan

    except Exception as e:
        print(f"⚠ Subscription plan check failed: {e}")
        return "free"


def get_device_quota(device_id: str) -> dict:
    usage = load_quota_usage()
    month = month_key()
    device_key = device_id or "anonymous"

    plan = _get_subscription_plan(device_key)
    limit = _plan_limit(plan)
    current = usage.get(month, {}).get(device_key, 0)

    return {
        "plan": plan,
        "used_today": current,        # kept for desktop compatibility
        "daily_limit": limit,         # kept for desktop compatibility
        "remaining_today": max(0, limit - current),
        "period": "month",
    }


def enforce_and_increment_quota(device_id: str) -> dict:
    if os.environ.get("DISABLE_QUOTA", "").lower() in ("1", "true", "yes"):
        return {
            "allowed": True,
            "plan": "dev",
            "used_today": 0,
            "daily_limit": 999999,
            "remaining_today": 999999,
            "period": "month",
            "message": "Dev quota disabled",
        }

    usage = load_quota_usage()
    month = month_key()
    device_key = device_id or "anonymous"

    # Keep only current month to avoid file growth.
    if month not in usage:
        usage = {month: {}}

    current = usage.get(month, {}).get(device_key, 0)

    plan = _get_subscription_plan(device_key)
    limit = _plan_limit(plan)

    if current >= limit:
        plan_label = plan.capitalize()
        return {
            "allowed": False,
            "plan": plan,
            "used_today": current,
            "daily_limit": limit,
            "remaining_today": 0,
            "period": "month",
            "message": f"You’ve used your {plan_label} plan scans for this month. Upgrade for more scans.",
        }

    usage.setdefault(month, {})
    usage[month][device_key] = current + 1
    save_quota_usage(usage)

    return {
        "allowed": True,
        "plan": plan,
        "used_today": current + 1,
        "daily_limit": limit,
        "remaining_today": max(0, limit - (current + 1)),
        "period": "month",
    }

def preprocess_for_whatfontis(image_b64: str) -> str:
    try:
        from PIL import Image, ImageEnhance, ImageOps

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes))

        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Sharpness(img).enhance(1.5)

        img_inv = ImageOps.invert(img)
        bbox = img_inv.getbbox()

        if bbox:
            w, h = img.size
            pad = 20
            x0 = max(0, bbox[0] - pad)
            y0 = max(0, bbox[1] - pad)
            x1 = min(w, bbox[2] + pad)
            y1 = min(h, bbox[3] + pad)
            img = img.crop((x0, y0, x1, y1))

        w, h = img.size
        if w < 600 or h < 100:
            scale = max(600 / max(w, 1), 100 / max(h, 1), 2.0)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    except Exception as e:
        print(f"⚠ WhatFontIs preprocessing error: {e}")
        return image_b64


def make_original_jpeg(image_b64: str) -> str:
    try:
        from PIL import Image

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes))

        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    except Exception:
        return image_b64


def normalize_font_name(title: str) -> str:
    clean = re.split(
        r'[-\s]+(Regular|Bold|Black|Light|Thin|Medium|Variable|'
        r'ExtraBold|SemiBold|ExtraLight|Heavy|Italic|Semi|'
        r'Oblique|Book|Demi|Ultra|Condensed|Cond|Extended|'
        r'Narrow|Wide|Display|Text|Caption|Rounded|'
        r'\.otf|\.ttf|\d{3,})',
        title,
        flags=re.IGNORECASE,
    )[0].strip()

    clean = re.sub(r'\s*\([^)]*\)', '', clean)
    clean = re.sub(
        r'\s*(regular|FFU|variable|semi|otf|ttf)\s*$',
        '',
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(
        r'\s*(MT|Std|LT|OT|Pro|Com|W1G|ITC|URW|EF|FS|FF)$',
        '',
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r'([a-z])([A-Z])', r'\1 \2', clean)
    clean = re.sub(
        r'\s*(MT|Std|LT|OT|Pro|Com|W1G)$',
        '',
        clean,
        flags=re.IGNORECASE,
    ).strip()

    return clean


def score_and_rank(all_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    family_scores = defaultdict(lambda: {"score": 0, "sources": set(), "best_title": ""})

    for i, r in enumerate(all_results):
        title = r.get("title", "")
        source = r.get("_source", "a")
        family = normalize_font_name(title).lower()

        if not family:
            continue

        entry = family_scores[family]
        entry["score"] += 10
        entry["score"] += max(1, 20 - i % 20)
        entry["sources"].add(source)

        if not entry["best_title"]:
            entry["best_title"] = title

    for family, entry in family_scores.items():
        if len(entry["sources"]) > 1:
            entry["score"] += 15

    ranked = sorted(family_scores.items(), key=lambda x: -x[1]["score"])

    scored = []
    for family, entry in ranked:
        scored.append({
            "title": entry["best_title"],
            "score": entry["score"],
            "consensus": len(entry["sources"]),
        })

    return scored


def call_whatfontis(img_data: str, label: str) -> list[dict[str, Any]]:
    if not WHATFONTIS_API_KEY:
        return []

    try:
        payload = urlencode({
            "API_KEY": WHATFONTIS_API_KEY,
            "IMAGEBASE64": "1",
            "urlimagebase64": img_data,
            "NOTTEXTBOXSDETECTION": "1",
            "limit": "20",
        })

        response = requests.post(
            "https://www.whatfontis.com/api2/",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=25,
        )

        if response.status_code != 200:
            print(f"⚠ WhatFontIs ({label}): HTTP {response.status_code}")
            return []

        results = response.json() or []

        for r in results:
            r["_source"] = label

        if results:
            top3 = [r.get("title", "?") for r in results[:3]]
            print(f"✓ WhatFontIs ({label}): {len(results)} results — {', '.join(top3)}")

        return results

    except Exception as e:
        print(f"⚠ WhatFontIs ({label}): {e}")
        return []


def run_whatfontis_pipeline(image_b64: str) -> list[dict[str, Any]]:
    print("\n=== Font Identification (server parallel) ===")
    start = time.time()

    wfi_original = make_original_jpeg(image_b64)
    wfi_enhanced = preprocess_for_whatfontis(image_b64)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_orig = pool.submit(call_whatfontis, wfi_original, "original")
        future_enh = pool.submit(call_whatfontis, wfi_enhanced, "enhanced")

        results_orig = future_orig.result()
        results_enh = future_enh.result()

    all_results = results_orig + results_enh
    elapsed = time.time() - start

    if not all_results:
        print("⚠ WhatFontIs returned no results")
        return []

    scored = score_and_rank(all_results)

    print(f"\nScored candidates ({elapsed:.1f}s):")
    for i, s in enumerate(scored[:8]):
        consensus = "✦" if s["consensus"] > 1 else " "
        print(f"  {i + 1}. {s['title']:30s} score={s['score']:3d} {consensus}")

    return scored[:10]



def _norm_font_name(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

def _build_google_font_index() -> dict:
    try:
        from google_fonts_catalog import get_catalog
        catalog = get_catalog()
        idx = {}
        for key, fonts in catalog.items():
            if key.startswith("_") or not isinstance(fonts, list):
                continue
            for font in fonts:
                idx[_norm_font_name(font)] = font
        return idx
    except Exception as e:
        print(f"⚠ Google font index failed: {e}")
        return {}

def _build_fontshare_index() -> dict:
    try:
        rows = load_json_file("fontshare_catalog.json", [])
        idx = {}
        if isinstance(rows, list):
            for item in rows:
                name = item.get("name", "")
                if name:
                    idx[_norm_font_name(name)] = item
        return idx
    except Exception as e:
        print(f"⚠ Fontshare index failed: {e}")
        return {}

def validate_free_alternatives(data: dict) -> dict:
    """Keep only alternatives that really exist in Google Fonts or Fontshare."""
    alternatives = data.get("alternatives") or data.get("free_alternatives") or []

    if not isinstance(alternatives, list):
        data["alternatives"] = []
        data["free_alternatives"] = []
        return data

    google_idx = _build_google_font_index()
    fontshare_idx = _build_fontshare_index()

    valid = []
    seen = set()

    for alt in alternatives:
        if not isinstance(alt, dict):
            continue

        raw_name = alt.get("name") or alt.get("font") or alt.get("family")
        key = _norm_font_name(raw_name)

        if not key or key in seen:
            continue

        similarity = alt.get("similarity", alt.get("score", 0))

        if key in google_idx:
            name = google_idx[key]
            valid.append({
                "name": name,
                "source": "Google Fonts",
                "similarity": similarity,
                "url": f"https://fonts.google.com/specimen/{name.replace(' ', '+')}",
            })
            seen.add(key)
            continue

        if key in fontshare_idx:
            item = fontshare_idx[key]
            name = item.get("name", raw_name)
            valid.append({
                "name": name,
                "source": "Fontshare",
                "similarity": similarity,
                "url": item.get("url") or f"https://www.fontshare.com/fonts/{item.get('slug', '')}",
            })
            seen.add(key)
            continue

        print(f"  ✗ Dropped invalid free alternative: {raw_name}")

    # If the AI returned invalid/hallucinated alternatives and too few remain,
    # fill the missing slots with real Google Fonts from the same category.
    if len(valid) < 3:
        try:
            from google_fonts_catalog import get_fonts_by_category

            category = str(data.get("category") or "sans-serif").lower()
            original_key = _norm_font_name(data.get("original_font", ""))

            fallback_fonts = get_fonts_by_category(category, limit=60)

            for font_name in fallback_fonts:
                key = _norm_font_name(font_name)

                if not key or key in seen or key == original_key:
                    continue

                valid.append({
                    "name": font_name,
                    "source": "Google Fonts",
                    "similarity": max(55, 78 - (len(valid) * 4)),
                    "url": f"https://fonts.google.com/specimen/{font_name.replace(' ', '+')}",
                })
                seen.add(key)

                if len(valid) >= 6:
                    break

        except Exception as e:
            print(f"  ⚠ Could not fill fallback alternatives: {e}")

    data["alternatives"] = valid[:6]
    data["free_alternatives"] = valid[:6]
    return data

def make_prompt(script: str, candidates: list[dict[str, Any]]) -> str:
    font_tags = load_json_file("font_tags.json", {})
    fontshare_catalog = load_json_file("fontshare_catalog.json", [])

    ranked_parts = []
    for c in candidates[:8]:
        title = c.get("title", "?")
        score = c.get("score", 0)
        consensus = c.get("consensus", 0)

        part = f"{title} (score:{score}"
        if consensus > 1:
            part += " cross-validated"
        part += ")"
        ranked_parts.append(part)

    ranked_text = ", ".join(ranked_parts)

    return f"""
You are FontDrop, an expert font identification engine.

Task:
Identify the font in the image and suggest the closest free alternatives.

Also extract sample_text: the exact visible text from the selected image. Preserve capitalization and punctuation when possible. If unreadable, return an empty string. Do not invent text.

Important:
- Use the WhatFontIs ranked candidates as strong hints.
- Prefer the visually closest font, not only the highest score.
- Always include 5-6 free alternatives when possible.
- Free alternatives must be real fonts from Google Fonts or Fontshare only.
- Do not list commercial/paid WhatFontIs candidates as free alternatives unless they are also available from Google Fonts or Fontshare.
- Return JSON only. No markdown.

Script: {script}

WhatFontIs ranked candidates:
{ranked_text}

Available font tags:
{json.dumps(font_tags if isinstance(font_tags, dict) else {}, ensure_ascii=False)[:6000]}

Fontshare catalog sample:
{json.dumps(fontshare_catalog[:300] if isinstance(fontshare_catalog, list) else fontshare_catalog, ensure_ascii=False)[:6000]}

Return this JSON schema:
{{
  "original_font": "Font Name",
  "sample_text": "Exact visible text from the image",
  "script": "{script}",
  "category": "sans-serif | serif | display | script | monospace | other",
  "confidence": "low | medium | high",
  "download_url": "https://...",
  "alternatives": [
    {{
      "name": "Alternative Font",
      "source": "Google Fonts | Fontshare | Open Source | Other",
      "similarity": 95,
      "url": "https://..."
    }}
  ],
  "other_matches": ["Font 1", "Font 2"],
  "notes": "short explanation"
}}
""".strip()



@app.post("/api/identify")
def identify(req: IdentifyRequest, x_fontdrop_secret: str | None = Header(default=None)):
    verify_api_secret(x_fontdrop_secret)

    if not req.image_base64:
        raise HTTPException(status_code=400, detail="Missing image_base64")

    try:
        raw = base64.b64decode(req.image_base64, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image_base64")

    if not gemini_client:
        raise HTTPException(status_code=500, detail="Gemini API key is not configured")

    image_hash = hashlib.sha256(raw).hexdigest()

    quota = enforce_and_increment_quota(req.device_id or "anonymous")
    if not quota.get("allowed"):
        usage_payload = {
            "plan": quota["plan"],
            "used_today": quota["used_today"],
            "daily_limit": quota["daily_limit"],
            "remaining_today": quota["remaining_today"],
        }

        log_usage_event(
            device_id=req.device_id or "anonymous",
            action="identify",
            status="daily_limit_reached",
            app_version=req.app_version,
            script=req.script or "latin",
            error="daily_limit_reached",
            usage=usage_payload,
        )

        return {
            "ok": False,
            "error": "daily_limit_reached",
            "message": quota["message"],
            "usage": usage_payload,
        }

    candidates = run_whatfontis_pipeline(req.image_base64)
    prompt = make_prompt(req.script or "latin", candidates)

    try:
        print("\n=== AI: Identify + Alternatives (server) ===")
        print("WhatFontIs ranked:", ", ".join([c.get("title", "?") for c in candidates[:8]]))
        print(f"→ Calling Gemini ({len(prompt)} chars prompt)...")

        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": req.image_base64,
                            }
                        },
                    ],
                }
            ],
        )

        text = resp.text.strip()

        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()

        data = json.loads(text)
        data = validate_free_alternatives(data)
        data["_engine"] = "gemini"
        data["_image_hash"] = image_hash
        data["_mode"] = "server"
        data["_candidates"] = candidates[:8]

        usage_payload = {
            "plan": quota["plan"],
            "used_today": quota["used_today"],
            "daily_limit": quota["daily_limit"],
            "remaining_today": quota["remaining_today"],
        }

        log_usage_event(
            device_id=req.device_id or "anonymous",
            action="identify",
            status="success",
            app_version=req.app_version,
            script=req.script or "latin",
            font_detected=data.get("original_font"),
            usage=usage_payload,
        )

        return {
            "ok": True,
            "result": data,
            "usage": usage_payload,
        }

    except json.JSONDecodeError:
        log_usage_event(
            device_id=req.device_id or "anonymous",
            action="identify",
            status="error",
            app_version=req.app_version,
            script=req.script or "latin",
            error="Gemini returned invalid JSON",
        )
        raise HTTPException(status_code=502, detail="Gemini returned invalid JSON")
    except Exception as e:
        log_usage_event(
            device_id=req.device_id or "anonymous",
            action="identify",
            status="error",
            app_version=req.app_version,
            script=req.script or "latin",
            error=f"Gemini failed: {str(e)}",
        )
        raise HTTPException(status_code=502, detail=f"Gemini failed: {str(e)}")
