"""Google Fonts catalog — fetch, cache, and search real font names.

On first run, fetches the full catalog from Google Fonts CSS API
and caches it to ~/.fontdrop/google_fonts.json.

Usage:
    from google_fonts_catalog import get_fonts_by_category, validate_font, get_catalog
"""
import json
import os
import time
import requests

CACHE_DIR = os.path.expanduser("~/.fontdrop")
CACHE_FILE = os.path.join(CACHE_DIR, "google_fonts.json")
CACHE_MAX_AGE = 7 * 24 * 3600  # refresh weekly

# ── In-memory cache (loaded once per session) ──
_CATALOG_CACHE = None

# ── Curated fallback list (if API fetch fails) ──
# Top ~150 Google Fonts by category, guaranteed to exist
FALLBACK_CATALOG = {
    "sans-serif": [
        "Roboto", "Open Sans", "Noto Sans", "Montserrat", "Lato", "Poppins",
        "Inter", "Oswald", "Raleway", "Nunito", "Ubuntu", "Rubik",
        "Work Sans", "Nunito Sans", "Fira Sans", "Barlow", "Mulish",
        "Kanit", "Manrope", "Cabin", "DM Sans", "Karla", "Arimo",
        "Exo 2", "Dosis", "Oxygen", "Hind", "Asap", "Overpass",
        "Catamaran", "Quicksand", "Source Sans 3", "Mukta", "Questrial",
        "Varela Round", "Comfortaa", "Lexend", "Jost", "Outfit",
        "Sora", "Albert Sans", "Figtree", "Geist", "Onest",
        "Plus Jakarta Sans", "Red Hat Display", "Urbanist",
        "Public Sans", "Commissioner", "Space Grotesk", "Archivo",
    ],
    "serif": [
        "Roboto Slab", "Merriweather", "Playfair Display", "Lora",
        "PT Serif", "Noto Serif", "Libre Baskerville", "EB Garamond",
        "Source Serif 4", "Bitter", "Crimson Text", "Crimson Pro",
        "Cormorant Garamond", "Arvo", "Zilla Slab", "Vollkorn",
        "DM Serif Display", "DM Serif Text", "Spectral", "Cardo",
        "Old Standard TT", "Gentium Book Plus", "Alegreya",
        "Josefin Slab", "Noticia Text", "Brygada 1918",
        "IBM Plex Serif", "Fraunces", "Literata", "Newsreader",
        "Bodoni Moda", "Libre Caslon Text", "Cormorant",
        "Young Serif", "Instrument Serif",
    ],
    "display": [
        "Bebas Neue", "Lobster", "Pacifico", "Righteous",
        "Abril Fatface", "Alfa Slab One", "Permanent Marker",
        "Passion One", "Bungee", "Fredoka One", "Russo One",
        "Press Start 2P", "Special Elite", "Monoton", "Orbitron",
        "Black Ops One", "Rampart One", "Silkscreen", "Pixelify Sans",
        "VT323", "Bangers", "Bungee Shade", "Creepster",
        "Faster One", "Frijole", "Luckiest Guy", "Ultra",
        "Bowlby One SC", "Bungee Inline", "Climate Crisis",
        "Nabla", "Rubik Glitch", "Rubik Vinyl", "Rubik Wet Paint",
        "Rubik 80s Fade", "Syne", "Big Shoulders Display",
    ],
    "handwriting": [
        "Caveat", "Dancing Script", "Indie Flower", "Shadows Into Light",
        "Satisfy", "Kalam", "Architects Daughter", "Patrick Hand",
        "Gloria Hallelujah", "Sacramento", "Great Vibes",
        "Covered By Your Grace", "Handlee", "Neucha", "Rock Salt",
        "Reenie Beanie", "Gochi Hand", "Just Another Hand",
        "Coming Soon", "Nothing You Could Do", "Zeyada",
        "Homemade Apple", "Amatic SC", "La Belle Aurore",
        "Waiting for the Sunrise", "Loved by the King",
        "Permanent Marker", "Kaushan Script",
    ],
    "monospace": [
        "Roboto Mono", "Source Code Pro", "Fira Code", "JetBrains Mono",
        "IBM Plex Mono", "Space Mono", "Courier Prime", "Inconsolata",
        "Ubuntu Mono", "Noto Sans Mono", "DM Mono", "Red Hat Mono",
        "Azeret Mono", "Cutive Mono", "Anonymous Pro", "Share Tech Mono",
        "Overpass Mono", "Major Mono Display", "Xanh Mono",
        "Syne Mono", "Martian Mono", "Geist Mono",
    ],
}


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_catalog() -> dict:
    """Fetch full Google Fonts catalog via the CSS API.

    Tests each font name from the fallback list and discovers
    additional fonts from the webfonts API (no key needed for CSS).
    Returns {category: [font_names]}.
    """
    print("  Fetching Google Fonts catalog...")

    # Strategy: try the Google Fonts metadata endpoint
    # (no API key needed for this one)
    try:
        resp = requests.get(
            "https://fonts.google.com/metadata/fonts",
            timeout=15,
            headers={"User-Agent": "FontDrop/1.0"},
        )
        if resp.status_code == 200:
            # Response starts with )]}'  (JSONP protection)
            text = resp.text
            if text.startswith(")]}'"):
                text = text[4:].strip()
            data = json.loads(text)

            catalog = {}
            font_subsets = {}  # {font_name: [subsets]}
            families = data.get("familyMetadataList", [])
            # Debug: show first entry structure
            if families:
                first = families[0]
                print(f"  → Metadata keys: {list(first.keys())[:10]}")
                for k in ["subsets", "scripts", "languages"]:
                    if k in first:
                        val = first[k]
                        if isinstance(val, dict):
                            print(f"  → {k} (dict): {list(val.keys())[:5]}")
                        elif isinstance(val, list):
                            print(f"  → {k} (list): {val[:5]}")
            for fam in families:
                name = fam.get("family", "")
                cat = fam.get("category", "sans-serif").lower().replace(" ", "-")
                subsets = []
                # Try different field names for subsets
                raw = fam.get("subsets") or fam.get("scripts") or {}
                if isinstance(raw, dict):
                    subsets = list(raw.keys())
                elif isinstance(raw, list):
                    subsets = raw
                # Also check languages field
                if not subsets:
                    langs = fam.get("languages") or fam.get("language") or []
                    if isinstance(langs, list):
                        subsets = langs
                if cat not in catalog:
                    catalog[cat] = []
                catalog[cat].append(name)
                if subsets:
                    font_subsets[name] = subsets

            catalog["_subsets"] = font_subsets
            total = sum(len(v) for k, v in catalog.items() if k != "_subsets" and isinstance(v, list))
            subset_count = len(font_subsets)
            # Debug: show Hebrew font count
            hebrew_count = sum(1 for s in font_subsets.values() if "hebrew" in [x.lower() for x in s])
            print(f"  ✓ Fetched {total} fonts, {subset_count} with subset data, {hebrew_count} support Hebrew")
            return catalog
    except Exception as e:
        print(f"  ⚠ Metadata fetch failed: {e}")

    # Fallback: return curated list
    print("  ⚠ Using curated fallback catalog (~150 fonts)")
    return FALLBACK_CATALOG


def get_catalog(force_refresh=False) -> dict:
    """Get catalog (memory → disk cache → fetch)."""
    global _CATALOG_CACHE
    _ensure_cache_dir()

    # 1. In-memory cache (instant)
    if not force_refresh and _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    # 2. Disk cache
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            age = time.time() - os.path.getmtime(CACHE_FILE)
            if age < CACHE_MAX_AGE:
                with open(CACHE_FILE) as f:
                    _CATALOG_CACHE = json.load(f)
                return _CATALOG_CACHE
        except Exception:
            pass

    # 3. Fetch fresh
    catalog = fetch_catalog()
    _CATALOG_CACHE = catalog

    # Save to disk
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(catalog, f)
    except Exception:
        pass

    return _CATALOG_CACHE


def get_fonts_by_category(category: str, limit: int = 0) -> list:
    """Get font names for a category.

    Args:
        category: "serif", "sans-serif", "display", "handwriting", "monospace"
        limit: max fonts to return (0 = all)
    """
    catalog = get_catalog()

    # Normalize category name
    cat = category.lower().replace(" ", "-")
    # Map common AI output names
    cat_map = {
        "sans": "sans-serif",
        "sans-serif": "sans-serif",
        "sansserif": "sans-serif",
        "script": "handwriting",
        "mono": "monospace",
        "display": "display",
        "decorative": "display",
    }
    cat = cat_map.get(cat, cat)

    fonts = catalog.get(cat, [])
    if limit > 0:
        return fonts[:limit]
    return fonts


def validate_font(font_name: str) -> bool:
    """Check if a font name exists in Google Fonts catalog."""
    catalog = get_catalog()
    all_fonts = []
    for fonts in catalog.values():
        all_fonts.extend(fonts)

    # Case-insensitive match
    lower_fonts = {f.lower(): f for f in all_fonts}
    return font_name.lower() in lower_fonts


def get_correct_name(font_name: str) -> str:
    """Get the correctly-cased font name, or None if not found."""
    catalog = get_catalog()
    lower_fonts = {}
    for fonts in catalog.values():
        for f in fonts:
            lower_fonts[f.lower()] = f

    return lower_fonts.get(font_name.lower())


def get_category_for_font(font_name: str) -> str:
    """Get the category of a font, or None."""
    catalog = get_catalog()
    for cat, fonts in catalog.items():
        if cat == "_subsets":
            continue
        if isinstance(fonts, list) and font_name.lower() in [f.lower() for f in fonts]:
            return cat
    return None


def get_fonts_by_subset(subset: str, limit: int = 50) -> list:
    """Get fonts that support a specific script/language.

    Args:
        subset: "hebrew", "arabic", "cyrillic", "chinese-simplified",
                "japanese", "korean", "greek", "thai", "devanagari", etc.
        limit: max fonts to return
    """
    catalog = get_catalog()
    font_subsets = catalog.get("_subsets", {})

    matching = []
    for font_name, subsets in font_subsets.items():
        if subset.lower() in [s.lower() for s in subsets]:
            matching.append(font_name)

    return matching[:limit] if limit else matching


# ── CLI test ──
if __name__ == "__main__":
    import sys

    catalog = get_catalog(force_refresh="--refresh" in sys.argv)

    print("\n  Google Fonts Catalog:")
    for cat, fonts in sorted(catalog.items()):
        print(f"    {cat}: {len(fonts)} fonts")
    total = sum(len(v) for v in catalog.values())
    print(f"    TOTAL: {total} fonts")

    # Test validation
    test_fonts = ["Roboto", "Open Sans", "MADE Tommy", "Fake Font 123",
                  "Montserrat", "press start 2p", "Courier Prime"]
    print("\n  Validation test:")
    for f in test_fonts:
        exists = validate_font(f)
        correct = get_correct_name(f) if exists else None
        cat = get_category_for_font(f) if exists else None
        status = f"✓ {correct} [{cat}]" if exists else "✗ NOT FOUND"
        print(f"    {f}: {status}")
