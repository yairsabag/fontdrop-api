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

load_dotenv()

APP_VERSION = "1.0.6"
FREE_DAILY_SCAN_LIMIT = 10

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WHATFONTIS_API_KEY = os.getenv("WHATFONTIS_API_KEY", "")
FONTDROP_API_SECRET = os.getenv("FONTDROP_API_SECRET", "")
FONTDROP_CLIENT_TOKEN = os.getenv("FONTDROP_CLIENT_TOKEN", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

app = FastAPI(title="FontDrop API", version=APP_VERSION)

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


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
        "version": APP_VERSION,
        "gemini_configured": bool(GEMINI_API_KEY),
        "whatfontis_configured": bool(WHATFONTIS_API_KEY),
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


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def get_device_quota(device_id: str) -> dict:
    usage = load_quota_usage()
    day = today_key()
    device_key = device_id or "anonymous"

    current = usage.get(day, {}).get(device_key, 0)

    return {
        "plan": "free",
        "used_today": current,
        "daily_limit": FREE_DAILY_SCAN_LIMIT,
        "remaining_today": max(0, FREE_DAILY_SCAN_LIMIT - current),
    }


def enforce_and_increment_quota(device_id: str) -> dict:
    usage = load_quota_usage()
    day = today_key()
    device_key = device_id or "anonymous"

    # Keep only today's usage to avoid file growth
    if day not in usage:
        usage = {day: {}}

    current = usage.get(day, {}).get(device_key, 0)

    if current >= FREE_DAILY_SCAN_LIMIT:
        return {
            "allowed": False,
            "plan": "free",
            "used_today": current,
            "daily_limit": FREE_DAILY_SCAN_LIMIT,
            "remaining_today": 0,
            "message": "You’ve used your 10 free scans for today. Your limit resets tomorrow, or you can upgrade for more scans.",
        }

    usage.setdefault(day, {})
    usage[day][device_key] = current + 1
    save_quota_usage(usage)

    return {
        "allowed": True,
        "plan": "free",
        "used_today": current + 1,
        "daily_limit": FREE_DAILY_SCAN_LIMIT,
        "remaining_today": max(0, FREE_DAILY_SCAN_LIMIT - (current + 1)),
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

Important:
- Use the WhatFontIs ranked candidates as strong hints.
- Prefer the visually closest font, not only the highest score.
- Always include free alternatives when possible.
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
        return {
            "ok": False,
            "error": "daily_limit_reached",
            "message": quota["message"],
            "usage": {
                "plan": quota["plan"],
                "used_today": quota["used_today"],
                "daily_limit": quota["daily_limit"],
                "remaining_today": quota["remaining_today"],
            },
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
        data["_engine"] = "gemini"
        data["_image_hash"] = image_hash
        data["_mode"] = "server"
        data["_candidates"] = candidates[:8]

        return {
            "ok": True,
            "result": data,
            "usage": {
                "plan": quota["plan"],
                "used_today": quota["used_today"],
                "daily_limit": quota["daily_limit"],
                "remaining_today": quota["remaining_today"],
            },
        }

    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Gemini returned invalid JSON")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini failed: {str(e)}")
