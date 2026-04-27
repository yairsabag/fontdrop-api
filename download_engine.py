"""
Google Fonts Download Engine
3 methods to reliably download fonts:
  1. GitHub static files (google/fonts repo + notofonts repo)
  2. CSS API with old User-Agent -> direct TTF URLs
  3. ZIP download from fonts.google.com (extracts static only)
"""
import os
import re
import io
import zipfile
import subprocess
import requests
from urllib.parse import quote

from config import FONTS_DIR, TTF_USER_AGENT

# Valid magic bytes for desktop-usable font formats
MAGIC_TTF = b'\x00\x01\x00\x00'
MAGIC_OTF = b'OTTO'
MAGIC_TTC = b'ttcf'
VALID_MAGIC = (MAGIC_TTF, MAGIC_OTF, MAGIC_TTC)


def _session():
    """Create a requests session with TTF-requesting User-Agent."""
    s = requests.Session()
    s.headers.update({"User-Agent": TTF_USER_AGENT})
    return s


def _is_real_font(data):
    """Check if binary data starts with valid font magic bytes."""
    return len(data) > 1000 and data[:4] in VALID_MAGIC


def _name_variations(font_name):
    """
    Generate possible Google Fonts family name variations.
    Handles cases like:
      "Noto Sans Bengali Regular" -> tries progressively shorter names
      "OpenSans-Bold" -> splits CamelCase and strips weight
    """
    WEIGHT_SUFFIXES = [
        "Thin", "ExtraLight", "UltraLight", "Light", "Regular", "Normal",
        "Medium", "SemiBold", "DemiBold", "Bold", "ExtraBold", "UltraBold",
        "Black", "Heavy", "Italic", "Oblique", "Condensed", "Expanded",
        "Extra Light", "Semi Bold", "Demi Bold", "Extra Bold", "Ultra Bold",
        "Ultra Light",
    ]

    name = font_name.strip()
    base_names = [
        name,
        name.replace("-", " "),
        name.replace("_", " "),
        re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name),
    ]

    all_variations = []
    for base in base_names:
        all_variations.append(base)
        stripped = base
        for suffix in sorted(WEIGHT_SUFFIXES, key=len, reverse=True):
            pattern = re.compile(r'\s+' + re.escape(suffix) + r'$', re.IGNORECASE)
            new = pattern.sub('', stripped).strip()
            if new != stripped and new:
                stripped = new
                all_variations.append(stripped)
        words = stripped.split()
        if len(words) > 2:
            for i in range(len(words) - 1, 1, -1):
                all_variations.append(" ".join(words[:i]))

    seen = set()
    unique = []
    for v in all_variations:
        key = v.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(v.strip())
    return unique


def method1_github_static(family_name, dest_dir):
    """
    Download STATIC font from GitHub repos.
    Tries: google/fonts repo (static subfolder) -> notofonts repo.
    Static fonts work properly with macOS Font Book.
    """
    s = _session()
    no_space = family_name.replace(' ', '')
    slug = no_space.lower()

    urls_to_try = [
        # google/fonts repo - some fonts have static/ subfolder
        (f"https://raw.githubusercontent.com/google/fonts/main/ofl/{slug}"
         f"/static/{no_space}-Regular.ttf",
         f"{no_space}-Regular.ttf"),

        # google/fonts repo - root level static
        (f"https://raw.githubusercontent.com/google/fonts/main/ofl/{slug}"
         f"/{no_space}-Regular.ttf",
         f"{no_space}-Regular.ttf"),

        # google/fonts repo - apache license fonts
        (f"https://raw.githubusercontent.com/google/fonts/main/apache/{slug}"
         f"/{no_space}-Regular.ttf",
         f"{no_space}-Regular.ttf"),

        # notofonts repo - best source for Noto family
        (f"https://raw.githubusercontent.com/notofonts/notofonts.github.io"
         f"/main/fonts/{no_space}/hinted/ttf/{no_space}-Regular.ttf",
         f"{no_space}-Regular.ttf"),

        # notofonts unhinted
        (f"https://raw.githubusercontent.com/notofonts/notofonts.github.io"
         f"/main/fonts/{no_space}/unhinted/ttf/{no_space}-Regular.ttf",
         f"{no_space}-Regular.ttf"),
    ]

    os.makedirs(dest_dir, exist_ok=True)
    for url, filename in urls_to_try:
        try:
            r = s.get(url, timeout=15)
            if r.status_code == 200 and _is_real_font(r.content):
                filepath = os.path.join(dest_dir, filename)
                with open(filepath, 'wb') as f:
                    f.write(r.content)
                return [filepath]
        except Exception:
            continue
    return []


def method2_css_api(family_name, dest_dir):
    """
    Fetch CSS from Google Fonts API with old Android User-Agent
    to get direct TTF URLs. Only saves files with valid magic bytes.
    Note: These may be subsets - method1 is preferred.
    """
    s = _session()
    css_url = (
        f"https://fonts.googleapis.com/css2?"
        f"family={quote(family_name, safe='')}"
    )
    try:
        r = s.get(css_url, timeout=12)
        if r.status_code != 200:
            return []
    except Exception:
        return []

    urls = re.findall(r'url\((https?://fonts\.gstatic\.com/[^)]+)\)', r.text)
    if not urls:
        return []

    os.makedirs(dest_dir, exist_ok=True)
    installed = []

    for font_url in urls:
        try:
            fr = s.get(font_url, timeout=15)
            if fr.status_code != 200:
                continue
            if not _is_real_font(fr.content):
                continue

            safe_name = re.sub(r'[^\w\-]', '_', family_name)
            idx = urls.index(font_url)
            ext = '.otf' if fr.content[:4] == MAGIC_OTF else '.ttf'
            filename = f"{safe_name}_{idx}{ext}"
            filepath = os.path.join(dest_dir, filename)

            with open(filepath, 'wb') as f:
                f.write(fr.content)
            installed.append(filepath)
        except Exception:
            continue

    return installed


def method3_zip_download(family_name, dest_dir):
    """
    Download ZIP from Google Fonts. Extracts ONLY static TTF/OTF files.
    Skips variable fonts (files with brackets in names).
    """
    s = _session()
    url = f"https://fonts.google.com/download?family={quote(family_name, safe='')}"
    try:
        r = s.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200 or not r.content or len(r.content) < 100:
            return []
        if r.content[:2] != b"PK":
            return []
    except Exception:
        return []

    os.makedirs(dest_dir, exist_ok=True)
    installed = []
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for entry in z.infolist():
                low = entry.filename.lower()
                # Only extract static TTF/OTF, skip variable fonts and __MACOSX
                if (low.endswith(('.ttf', '.otf'))
                        and '__macosx' not in low
                        and '[' not in entry.filename
                        and 'VariableFont' not in entry.filename):
                    basename = os.path.basename(entry.filename)
                    if basename:
                        data = z.read(entry)
                        if _is_real_font(data):
                            filepath = os.path.join(dest_dir, basename)
                            with open(filepath, 'wb') as f:
                                f.write(data)
                            installed.append(filepath)
    except Exception:
        return []
    return installed


def download_google_font(font_name, dest_dir=None):
    """
    Try all methods to download a Google Font.
    Downloads directly to ~/Library/Fonts for immediate use.
    Returns (files_list, matched_name) or ([], original_name).
    Order: GitHub static -> CSS API -> ZIP (static only)
    """
    if dest_dir is None:
        dest_dir = FONTS_DIR

    for name in _name_variations(font_name):
        for method in (method1_github_static, method2_css_api, method3_zip_download):
            files = method(name, dest_dir)
            if files:
                return files, name

    return [], font_name


def refresh_font_cache():
    """Refresh macOS font cache. Uses killall fontd on macOS 14+."""
    try:
        subprocess.run(["killall", "fontd"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(["atsutil", "databases", "-removeUser"],
                       capture_output=True, timeout=5)
        subprocess.run(["atsutil", "server", "-shutdown"],
                       capture_output=True, timeout=5)
        subprocess.run(["atsutil", "server", "-ping"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
