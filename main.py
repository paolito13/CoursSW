"""
CourSW.exe — Observateur d'annonces FiveM pour Seven Wands
Détecte automatiquement FiveM, capture les annonces, les envoie au site.
Compatible Windows 10/11. Aucune installation manuelle requise.
"""

import sys
import os
import subprocess
import importlib

# PyInstaller : certifi doit être localisé avant tout import réseau
if getattr(sys, 'frozen', False):
    _cert = os.path.join(sys._MEIPASS, 'certifi', 'cacert.pem')
    if os.path.isfile(_cert):
        os.environ['SSL_CERT_FILE']      = _cert
        os.environ['REQUESTS_CA_BUNDLE'] = _cert

# ── Auto-installer (lancé AVANT tout import externe) ─────────────────────────
REQUIRED = {
    "requests":   "requests",
    "mss":        "mss",
    "PIL":        "Pillow",
    "win32gui":   "pywin32",
    "winsdk":     "winsdk",
    "pystray":    "pystray",
    "psutil":     "psutil",
}

def _bootstrap():
    missing = []
    for mod, pkg in REQUIRED.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    # Fenêtre de progression minimaliste (avant tkinter complet)
    import tkinter as _tk
    root = _tk.Tk()
    root.title("CourSW — Installation")
    root.geometry("420x140")
    root.resizable(False, False)
    root.configure(bg="#0d1a1e")
    _tk.Label(root, text="⚙️  Première installation des composants…",
              font=("Segoe UI", 11, "bold"), bg="#0d1a1e", fg="#fde090").pack(pady=(22, 6))
    status = _tk.StringVar(value="Préparation…")
    _tk.Label(root, textvariable=status, font=("Segoe UI", 9),
              bg="#0d1a1e", fg="#90c8ff").pack()
    from tkinter import ttk
    bar = ttk.Progressbar(root, length=360, mode="determinate")
    bar.pack(pady=12)
    root.update()

    SLOW_PKGS = {"winsdk"}

    for i, pkg in enumerate(missing):
        if pkg in SLOW_PKGS:
            status.set(f"Compilation de {pkg} (5-10 min, ne pas fermer)…")
        else:
            status.set(f"Installation de {pkg}…")
        bar["value"] = int((i / len(missing)) * 90)
        root.update()
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet", "--disable-pip-version-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    bar["value"] = 100
    status.set("✅ Installation terminée. Démarrage…")
    root.update()
    import time; time.sleep(1)
    root.destroy()

_bootstrap()

# ── Imports normaux (toutes les deps sont garanties) ─────────────────────────
import time
import json
import threading
import webbrowser
import hashlib
import re
import ctypes
import tkinter as tk
from tkinter import ttk
from pathlib import Path

import io
import base64
import requests
import mss
from PIL import Image, ImageDraw
import win32gui
import win32con
import win32process
import pystray

# ── Windows OCR (natif Windows 10/11, aucun téléchargement) ──────────────────
try:
    import asyncio
    import winsdk  # optionnel — on tente l'OCR natif
    _USE_WIN_OCR = True
except ImportError:
    _USE_WIN_OCR = False

# Fallback : pytesseract si disponible
try:
    import pytesseract
    _USE_TESSERACT = True
except ImportError:
    _USE_TESSERACT = False

# ── Config ────────────────────────────────────────────────────────────────────
VERSION = "1.5.64"
SITE_URL       = "https://almanach-peh.vercel.app"
API_LINK       = f"{SITE_URL}/api/cours/link"
API_HEARTBEAT  = f"{SITE_URL}/api/cours/heartbeat"
API_ANNOUNCE   = f"{SITE_URL}/api/cours/announce"

TOKEN_FILE         = Path(os.environ.get("APPDATA", ".")) / "CourSW" / "token.json"
_BROWSER_FLAG_FILE = Path(os.environ.get("APPDATA", ".")) / "CourSW" / "_browser_opened.flag"
CAPTURE_INTERVAL   = 1.0
HEARTBEAT_INTERVAL = 30

# Zone capture initiale : large pour couvrir toute résolution
# Le popup est ensuite détecté et rogné automatiquement par couleur
CAP_RIGHT  = 0.38
CAP_BOTTOM = 0.55


# ── Windows OCR natif ─────────────────────────────────────────────────────────
async def _win_ocr_async(pil_img: Image.Image) -> str:
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import (
        BitmapDecoder, SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    )
    import io

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    data = buf.read()

    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED)

    engine = OcrEngine.try_create_from_language(Language("fr-FR")) \
          or OcrEngine.try_create_from_user_profile_languages() \
          or OcrEngine.try_create_from_language(Language("en-US"))

    result = await engine.recognize_async(bitmap)
    return result.text if result else ""

def _preprocess(pil_img: Image.Image) -> Image.Image:
    """Agrandissement ×2 uniquement — évite les artefacts du contraste sur le texte FiveM."""
    w, h = pil_img.size
    return pil_img.resize((w * 2, h * 2), Image.LANCZOS)


def ocr_image(pil_img: Image.Image) -> str:
    img = _preprocess(pil_img)
    if _USE_WIN_OCR:
        try:
            loop = asyncio.new_event_loop()
            text = loop.run_until_complete(
                asyncio.wait_for(_win_ocr_async(img), timeout=3.0)
            )
            loop.close()
            return text
        except Exception:
            pass

    if _USE_TESSERACT:
        try:
            return pytesseract.image_to_string(img, lang="fra+eng")
        except Exception:
            pass

    return ""


# ── FiveM window detection ────────────────────────────────────────────────────
FIVEM_EXES = {"fivem.exe", "fivem_b3095_gtaprocess.exe", "gta5.exe", "gtavlauncher.exe"}

def find_fivem_window():
    result = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            import psutil
            proc_name = psutil.Process(pid).name().lower()
            if not any(exe in proc_name for exe in FIVEM_EXES):
                # Fallback : titre de fenêtre
                title = win32gui.GetWindowText(hwnd).lower()
                if not ("fivem" in title or "gta" in title):
                    return
        except Exception:
            title = win32gui.GetWindowText(hwnd).lower()
            if not ("fivem" in title or "gta" in title):
                return
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > 400 and h > 300:
            result.append((hwnd, rect))
    win32gui.EnumWindows(cb, None)
    result.sort(key=lambda x: (x[1][2]-x[1][0]) * (x[1][3]-x[1][1]), reverse=True)
    return result[0] if result else None

def _detect_popup_crop(pil_img: Image.Image) -> Image.Image:
    """
    Détecte automatiquement le bord droit du popup d'annonce FiveM
    par sa couleur bleue distinctive (fond sombre bleu).
    Rogne l'image pour n'OCR que le popup, quelle que soit la résolution.
    """
    w, h = pil_img.size
    step = max(1, h // 50)   # ~50 lignes de sondage
    best_right = 0

    # Scan de droite à gauche : cherche la colonne la plus à droite
    # contenant des pixels typiques du popup FiveM (ancien: bleu foncé / nouveau: vert-teal foncé)
    for x in range(w - 1, 10, -1):
        hits = 0
        for y in range(0, h, step):
            try:
                px = pil_img.getpixel((x, y))
                r, g, b = px[0], px[1], px[2]
                # Détection générique : tout pixel coloré (non noir, non blanc)
                # Couvre toutes les couleurs d'année : vert (1re), violet (4e), bleu, rouge…
                max_c = max(r, g, b)
                min_c = min(r, g, b)
                is_popup = (max_c - min_c) > 25 and 35 < max_c < 220
                if is_popup:
                    hits += 1
            except Exception:
                pass
        if hits >= 3:
            best_right = x
            break

    if best_right > 80:
        # Ajoute 30px de marge droite et recadre
        crop_right = min(w, best_right + 30)
        return pil_img.crop((0, 0, crop_right, h))
    # Fallback : image entière
    return pil_img


def capture_region(rect):
    wx, wy, wx2, wy2 = rect
    ww, wh = wx2 - wx, wy2 - wy
    region = {
        "left":   wx,
        "top":    wy,
        "width":  int(ww * CAP_RIGHT),
        "height": int(wh * CAP_BOTTOM),
    }
    with mss.mss() as sct:
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return _detect_popup_crop(img)

# ── Parsing OCR → annonce structurée ─────────────────────────────────────────

import unicodedata as _ud

def _deaccent(s: str) -> str:
    return ''.join(c for c in _ud.normalize('NFD', s.lower()) if _ud.category(c) != 'Mn')

def _trigrams(s: str) -> set:
    s = ' ' + s + ' '
    return {s[i:i+3] for i in range(len(s) - 2)}

def _trigram_sim(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta and not tb: return 1.0
    if not ta or not tb:  return 0.0
    return len(ta & tb) / len(ta | tb)

def _best_canonical(raw: str, table: list[tuple[str, list[str]]]) -> str:
    """
    Retourne TOUJOURS l'une des valeurs canoniques de la table.
    Étape 1 : correspondance par mots-clés.
    Étape 2 : similarité trigrammes sur le label normalisé.
    Jamais de retour du texte OCR brut.
    """
    key = _deaccent(raw.strip())
    # Étape 1 — mots-clés
    best_label, best_score = table[0][0], 0
    for label, keywords in table:
        score = sum(1 for kw in keywords if kw in key)
        if score > best_score:
            best_label, best_score = label, score
    if best_score > 0:
        return best_label
    # Étape 2 — trigrammes sur le label canonique normalisé
    best_label, best_sim = table[0][0], -1.0
    for label, _ in table:
        sim = _trigram_sim(key, _deaccent(label))
        if sim > best_sim:
            best_label, best_sim = label, sim
    return best_label


# Salles officielles + leurs variantes OCR / abréviations
_ROOMS: list[tuple[str, list[str]]] = [
    ('La Cabane',                  ['cabane']),
    ('Salle Potions',                  ['cms', 'potion', 'potions', 'salle de potion']),
    ('Salle Créatures Magiques',   ['creature', 'creatur', 'magique', 'magiques', 'salle creature', 'magiwes', 'magiqye', 'magic&jues', 'creatures magic']),
    ('Serre 1',                    ['serre 1', 'serre1', 'serre', 'serrfs']),
    ('Serre 2',                    ['serre 2', 'serre2']),
    ('Serre 3',                    ['serre 3', 'serre3']),
    ('Serre 4',                    ['serre 4', 'serre4']),
    ('Salle DCFM (toilettes)',     ['dcfm', 'toilette', 'saile']),
    ('Salle Musique',              ['musique']),
    ('Salle Généraliste',          ['generaliste', 'general', 'generalist', 'generauste', 'generau', 'generaliete', 'classe generaliste', 'classe general', 'sat f general', '11 x club', '11x', 'x club', 'duel league', 'duel en groupe', 'capture de zone', 'saile generausie', 'saile generau', 'generausie', 'salle generauste', 'dans generauste', 'club serre', 'saile generausie dans']),
    ('Salle Potions',              ['salle potion', 'salle potions', 'potion', 'potions']),
    ('Salle de Duel',              ['duel', 'tolte', 'tour', 'tou-u-r', 'saue', 'salle', 'musiqye', 'ft-1palto', 'ft-1palt']),
    ('Salle de Littérature',       ['litter', 'littera', 'litterature', 'litteratur', 'literature']),
    ("Salle d'Etude de golmue",    ['golmue', 'golmu', 'etude de golm', 'study']),
]

def _normalize_room(raw: str) -> str:
    if not raw:
        return raw
    # Serre avec numéro : détection directe prioritaire
    m = re.search(r'serre\s*(\d)', _deaccent(raw))
    if m:
        return f'Serre {m.group(1)}'
    return _best_canonical(raw, _ROOMS)


# Matières officielles + leurs variantes OCR / abréviations
_SUBJECTS: list[tuple[str, list[str]]] = [
    ('Alchimie - Botanique', ['alchimie', 'botanique', 'alch']),
    ('Sorts',                ['sort', 'sorts', 'magie', 'sai']),
    ('Potions',              ['potion', 'potions']),
    ('Histoire de la Magie', ['histoire', 'hdm', 'hmd', 'hist']),
    ('Créatures Magiques',   ['creature', 'creatur', 'magique', 'magiques', 'triton', 'animaux', 'bestiaire']),
    ('Club',                 ['club']),
    ('Divers',               ['divers', 'hygiene', 'hygiène', 'initiation']),
]

def _normalize_subject(raw: str) -> str:
    if not raw:
        return raw
    return _best_canonical(raw, _SUBJECTS)


# Mots qui signalent la fin du nom d'auteur
_STOP = (
    r'[Cc]ours|[Tt]outes?|[Tt]ous|[Ll]es?|[Dd]ans|[Aa]ux?|[Dd]es?'
    r'|[Uu]ne?|[Ee]n|[Ss]a[lr][le]|[Ss]erre|[Pp]our|[Aa]vec|[Dd]e\b'
    # Matières / mots-clés FiveM fréquents après le nom
    r'|[Ss]orts?|[Pp]otions?|[Dd]ivers|[Cc]lubs?|[Hh][Dd][Mm]|[Aa]lchimie'
    r'|[Bb]otanique|[Aa]stronomie|[Tt]ransfiguration|[Mm][ée]tamorphose'
    r'|[Dd][ée]fense|[Dd]ivination|[Aa]rithmancie|[Ss]oins'
    r'|[Cc]r[eé]ature|[Mm]agique|[Cc]ours|[Hh]istoire|[Ll]itt[eé]rature|[Cc]learw(?:at)?'
    r'|[Dd]ernier|[Rr]appel|[Cc]ommence|[Dd][eé]bute|[Aa]nnonce|[Uu]rgent'
    r'|[Ff]action|[Ee]quipe|[Éé]quipe|[Gg]roupe|[Gg]uilde|[Cc]lan'
    r'|[Ll]a\b|[Ss]aut\b|[Cc]orrespondance|[Nn]umérolog|[Ii]nterpretation|[Ii]nterprétation'
    r'|[Cc]omplot|[Nn]yxie|[Ii]nitiation'
    # Salles (évite que "Duel" soit capturé comme nom)
    r'|[Dd]uel\b|[Gg]eneraliste|[Gg]énéraliste|[Gg]eneralust'
    # Tokens d'années (VII, EME, ERE, ANNEE) qui saignent dans l'auteur
    r'|[Vv]ii\b|[Ee]me\b|[Éé]me\b|[Ee]re\b|[Éé]re\b|[Aa]nn[eé]e\b|secatr[a-z]*|[Aa]u\b|zito'
    # Tokens OCR parasites tout-caps en début d'auteur (STERIJ, BARJNOV, LENFIEZ.D, etc.)
    r'|(?:[A-ZÀ-Ü]{2,}[A-ZÀ-Ü0-9]*\.?(?![a-zà-ü]))|[Vv][Oo][Nn]\b|[Bb]ataille\b'
    r'|[Tt]h[eé]rianthropes?|[Tt]h[eé]rianthrop|[Tt][Hh][ÉéEe][Rr][Ii][Aa][Nn][Tt][Hh][Rr][Oo][Pp][EeÉé][Ss]?'
)

# Année : tolère les typos OCR, chiffres romains, et format "Année: 1er" (label avant chiffre)
_ANN = r'ann[eéèêë]{1,2}e?s?'   # matche année/annee/ANNÉE/ANNEE avec re.IGNORECASE
_YEAR_RE = (
    rf'(?:toutes?\s+(?:les\s+)?{_ANN}'                                        # toutes les années
    rf'|\d+\s*(?:[eèêé]me?|[eèé]re?|[eè])\s+{_ANN}'                         # 4ème année / 1ère année
    rf'|[1I]\s*(?:[eèé]re?|[eè])\s+{_ANN}'                                   # 1ère / 1ere année
    rf'|(?:[eèé]re?)\s+{_ANN}'                                                # "ère année" seul (I perdu en OCR → 1ère implicite)
    rf'|(?:X|IX|VIII|VII|VI|V|IV|III|II)\s*(?:[eèêé]me?|[eèé]re?|[eè])?\s+{_ANN}' # X ème / V ème / IV ème année (X = OCR lit V comme X)
    rf'|{_ANN}\s*:?\s*\d+\s*(?:[eèêé]me?|[eèé]re?|[eè])?'                   # Année: 1er / ANNÉE 1ER
    r')'
)


def _clean_noise(s: str) -> str:
    """Supprime les caractères parasites OCR (lettres isolées, pas les mots courts utiles)."""
    # Ne supprime que les lettres VRAIMENT isolées (1 seul char), pas "la", "de", "le"…
    s = re.sub(r'(?<![a-zA-ZÀ-ü])[a-zA-Z](?![a-zA-ZÀ-ü])', ' ', s)
    return re.sub(r'\s{2,}', ' ', s).strip()


# Pivot strict : mots spécifiques aux salles, peu susceptibles d'apparaître dans les titres
# Sa[lru][lei]? couvre "Salle", "Sale", "Saue" (OCR "ll"→"u"), "Sall"…
_STRICT_ROOM = re.compile(
    r'(?:Sa[lru][lei]e?|S[ae]rre|Cabane|Donjon|For[eê]t|Terrain\s+[A-ZÀ-Üa-zà-ü]|Tour\s+[A-ZÀ-Üa-zà-ü])',
    re.IGNORECASE
)

# Pivot large : utilisé UNIQUEMENT dans la section après §SPLIT§
_WIDE_ROOM = re.compile(
    r'(?:Sa[lr][le]e?|Serre|Cabane|Donjon|For[eê]t'
    r"|La\s+[A-ZÀ-Ü]|Le\s+[A-ZÀ-Ü]|Les\s+[A-ZÀ-Ü]|L'[A-ZÀ-Ü]"
    r'|Au[x]?\s+[A-ZÀ-Ü]|Grand[e]?\s+[A-ZÀ-Ü]|Petit[e]?\s+[A-ZÀ-Ü]'
    r'|Tour\s+[A-ZÀ-Ü]|Terrain\s+[A-ZÀ-Ü])'
)


def _split_details(details: str, wide: bool = False) -> tuple[str, str]:
    """Extrait (room, subject) depuis la section détails."""
    details = _clean_noise(details)

    # Essai 1 : emoji 📖
    m_icon = re.search(r'📖\s*(.+?)$', details)
    if m_icon:
        return _clean_noise(details[:m_icon.start()]), m_icon.group(1).strip()

    # Essai 2 : groupe trailing = sujet (1-3 mots, commence par majuscule)
    m_subj = re.search(
        r'(?:\s|^)([A-ZÀ-Ü][a-zA-Zà-ü\-]+(?:\s+[a-zà-ü]\w*){0,2})\s*$',
        details
    )
    if m_subj and len(m_subj.group(1)) >= 3:
        return _clean_noise(details[:m_subj.start()]), m_subj.group(1).strip()

    return details, ""


def parse_announcement(text: str) -> dict | None:
    if not text.strip():
        return None

    joined = " ".join(text.split())

    # ── Normalisation OCR ──────────────────────────────────────────────────────
    # Supprime les overlays de performance (MSI Afterburner, RivaTuner, etc.)
    joined = re.sub(r'\bPL\s*:\s*', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bCPU\s*:\s*[\d/\., ]+', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bGPU\s*:\s*[\d/\., ]*', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bFPS\s*:\s*[\d/\., ]+', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bRAM\s*:\s*[\d/\., ]+', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bVRAM\s*:\s*[\d/\., ]+', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bIO\b', '10', joined)
    joined = re.sub(r'\bl0\b', '10', joined)
    # Corrections typos OCR fréquentes sur les noms de salles et mots-clés
    joined = re.sub(r'\bGENERAUSTE\b', 'GENERALISTE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bGENERALUSTE\b', 'GENERALISTE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGIWES\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGIQYE\b', 'MAGIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bHISIOIRES\b', 'HISTOIRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMUSQUE\b', 'MUSIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLITTERATURE\b', 'LITTÉRATURE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAUE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAIE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAILE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAI\s+F\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSERRFS\b', 'SERRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMUSIQYE\b', 'MUSIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAUSPOTI\w*\b', 'SALLE POTIONS', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bÉLÈVFS\b', 'ÉLÈVES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bCIN[OQ][UY]I[EÈ]ME\b', 'CINQUIÈME', joined, flags=re.IGNORECASE)
    # Artefact OCR d'emoji lu "ft-" en début de token (ex: ft-AMETITE → AMETITE)
    joined = re.sub(r'\bft-', '', joined, flags=re.IGNORECASE)
    # Strip du menu paramètres FiveM capturé par OCR (Manette, Clavier, Son, Caméra…)
    joined = re.sub(
        r'\bJeu\b.*?(?:Graphismes\s+avanc[eé]s?|Graphismes|Affichage)\b.*',
        '', joined, flags=re.IGNORECASE | re.DOTALL
    )
    # Barre séparatrice FiveM → marqueur §SPLIT§ (pivot le plus fiable)
    joined = re.sub(r'[─━]{3,}', ' §SPLIT§ ', joined)
    joined = re.sub(r'-{5,}', ' §SPLIT§ ', joined)
    joined = re.sub(r'\s\.\s', ' ', joined)
    # OCR lit souvent "/" comme " I " dans les titres FiveM
    joined = re.sub(r'(?<=[A-Za-zÀ-ü0-9])\s+I\s+(?=[A-ZÀ-Üa-zà-ü0-9])', ' / ', joined)
    # OCR fusionne "/ 3" en "13" (I+digit sans espace) → on restaure l'ordinal
    joined = re.sub(r'\b1(\d\s*(?:[eèê]me?|[eè]re?|e)\b)', r'\1', joined)
    # OCR lit "II" (2ème année) comme "11" — corrige avant extraction d'année
    joined = re.sub(r'\b11\s*(?=[eèêéE]me?\b)', '2 ', joined, flags=re.IGNORECASE)

    is_cours   = bool(re.search(r'ANNONCE\s+DE\s+COURS', joined, re.IGNORECASE))
    is_general = bool(re.search(r'ANNONCE\s+(?!DE\s+COURS)[A-ZÀ-Ü]', joined))

    if not is_cours and not is_general:
        return None

    # Extrait l'année depuis le texte COMPLET — elle apparaît souvent avant
    # "ANNONCE DE COURS" (colonne gauche du popup) et serait perdue sinon
    _year_hits_full = list(re.finditer(_YEAR_RE, joined, re.IGNORECASE))
    _year_from_header = _year_hits_full[0].group(0).strip() if _year_hits_full else ""
    # "ère Année" sans numéro (I perdu OCR) → "1ère Année"
    if _year_from_header and re.match(r'^[eèé]re?\s', _year_from_header, re.IGNORECASE):
        _year_from_header = '1' + _year_from_header

    # ══════════════════════════════════════════════════════════════════════════
    if is_cours:
        m = re.search(r'ANNONCE\s+DE\s+COURS\s*(.*)', joined, re.IGNORECASE)
        payload = m.group(1).strip() if m else joined

        # Normalise les tokens ALL CAPS en Title Case
        # Gère aussi les mots avec point interne (ex: CALAUDR.A → Calaudr.A)
        def _norm_tok(w: str) -> str:
            if len(w) <= 1:
                return w
            if '.' in w:
                parts = w.split('.')
                return '.'.join(
                    p.capitalize() if p and p.isupper() and re.fullmatch(r'[A-ZÀ-Üa-zà-ü\-]+', p) else p
                    for p in parts
                )
            # Normalise aussi les mots entre ponctuation ex: (FLAMETTE) → (Flamette)
            m = re.fullmatch(r'([^A-ZÀ-Üa-zà-ü]*)([A-ZÀ-Üa-zà-ü\-]{2,})([^A-ZÀ-Üa-zà-ü]*)', w)
            if m and m.group(2).isupper():
                return m.group(1) + m.group(2).capitalize() + m.group(3)
            return w
        payload = ' '.join(_norm_tok(w) for w in payload.split())

        # ── Auteur ────────────────────────────────────────────────────────────
        # Token nom : mot commençant par majuscule (Dupont) OU initiale seule (L / L.)
        # S'arrête aux abbréviations tout-caps (HDM, HMD…) et aux mots _STOP
        _NAME_TOK = r'(?:[A-ZÀ-Ü][A-ZÀ-Üa-zà-ü\'\-]+|[A-ZÀ-Ü]\.?(?=\s|$))'
        # Contractions tout-caps (C'EST, D'UNE…) → jamais un nom propre
        _ALL_CAPS_CONTRACTION = r"[A-ZÀ-Ü]'[A-ZÀ-Ü]{2,}"
        _NAME_STOP = rf'(?:{_STOP}|[A-ZÀ-Ü]{{2,}}(?![a-zà-ü])|{_ALL_CAPS_CONTRACTION})'
        author = ""
        m_a = re.search(
            rf'(?i:par)\.?\s+(?:(?:Pr|Dr|Mme|Mlle|M)\.?\s+)?'
            rf'({_NAME_TOK}'
            rf'(?:\s+(?!(?:{_NAME_STOP})\b){_NAME_TOK}){{0,2}})',
            payload
        )
        if m_a:
            author = m_a.group(1).strip()
            # Sécurité : retire les mots _STOP, contractions tout-caps, ou tokens all-caps en fin de nom
            author = re.sub(rf'\s+(?:{_STOP}|{_ALL_CAPS_CONTRACTION}|[A-ZÀ-Ü]{{3,}}(?![a-zà-ü]))$', '', author).strip()
            payload = payload[m_a.end():].strip()
            # Retire les caractères non-alpha en début de payload (ex: ".A Hdm…" → "Hdm…")
            payload = re.sub(r'^[^a-zA-ZÀ-ÿ(]+', '', payload)

        # ── Séparation description / détails ──────────────────────────────────
        message = ""
        year    = ""
        delay   = ""
        room    = ""
        subject = ""

        # ── Format v2 : emojis structurés (📚 matière / 🏛 salle / ⌛ délai) ─
        # OCR Windows peut détecter ces emojis Unicode natifs
        m_delay_e = re.search(r'⌛\s*(.+?)(?=📚|🏛|⌛|$)', payload)
        m_room_e  = re.search(r'🏛\s*(.+?)(?=📚|⌛|$)', payload)
        m_subj_e  = re.search(r'📚\s*(.+?)(?=🏛|⌛|$)', payload)
        emoji_anchors = [m for m in [m_delay_e, m_room_e, m_subj_e] if m]

        if emoji_anchors:
            # Parsing par emojis : le plus fiable quand OCR les capte
            if m_delay_e: delay   = m_delay_e.group(1).strip()
            if m_room_e:  room    = _normalize_room(m_room_e.group(1).strip())
            if m_subj_e:  subject = _normalize_subject(m_subj_e.group(1).strip())
            first_emoji_pos = min(m.start() for m in emoji_anchors)
            title_raw = payload[:first_emoji_pos].strip(" -—(,:")
            # Extrait "- Année: X" inline dans le titre
            m_annee = re.search(r'\s*[-–]\s*[Aa]nn[ée]e?\s*:\s*([^\-–]+?)(?=\s*[-–]|$)', title_raw)
            if m_annee:
                if not year: year = m_annee.group(1).strip()
                title_raw = (title_raw[:m_annee.start()] + title_raw[m_annee.end():]).strip(" -—")
            # Extrait "- Salle: X" inline dans le titre (backup si 🏛 raté)
            m_salle_inl = re.search(r'\s*[-–]\s*[Ss]alle\s*:\s*(.+?)(?=\s*[-–]|$)', title_raw)
            if m_salle_inl:
                if not room: room = _normalize_room(m_salle_inl.group(1).strip())
                title_raw = (title_raw[:m_salle_inl.start()] + title_raw[m_salle_inl.end():]).strip(" -—")
            # Retire le "Cours " initial redondant avec "ANNONCE DE COURS"
            title_raw = re.sub(r'^[Cc]ours\s+', '', title_raw).strip()
            # Retire la préposition initiale résiduelle (ex: "de Sort…" → "Sort…")
            title_raw = re.sub(r'^(?:dans|de|du|d\'|des|en|la|le|les|au[x]?)\s+', '', title_raw, flags=re.IGNORECASE)
            # "[Matière] : [Titre du cours]" (1er screenshot)
            m_col = re.match(r'^([^:]{1,40}):\s*(.+)$', title_raw, re.DOTALL)
            if m_col:
                if not subject:
                    subject = _normalize_subject(m_col.group(1).strip())
                message = m_col.group(2).strip()
            else:
                message = title_raw

        elif '§SPLIT§' in payload:
            # Format v1 : séparateur ─────── → §SPLIT§
            parts = payload.split('§SPLIT§', 1)
            message = parts[0].strip(" -—(,:")
            details_raw = parts[1].split('§SPLIT§')[0].strip()
            m_d = re.search(r'[Dd]ans\s+\d+\s+\w+(?:\s*\([^)]*\))?', details_raw)
            if m_d:
                delay = m_d.group(0)
                details_raw = details_raw[:m_d.start()].strip()
            year_hits = list(re.finditer(_YEAR_RE, details_raw, re.IGNORECASE))
            if year_hits:
                last_y = year_hits[-1]
                year = last_y.group(0).strip()
                details_raw = (details_raw[:last_y.start()] + " " + details_raw[last_y.end():]).strip()
            room, subject = _split_details(details_raw, wide=True)
            if subject:
                m_subj_prefix = re.match(
                    rf'^{re.escape(subject)}\s*[-–—]\s*', message, re.IGNORECASE
                )
                if m_subj_prefix:
                    message = message[m_subj_prefix.end():].strip(" -—():,")

        else:
            # Format v2 sans emojis (OCR a raté les icônes) OU format v1 sans séparateur
            # Délai : extrait en premier (ancre la plus fiable)
            m_d = re.search(r'[Dd]ans\s+\d+\s+\w+(?:\s*\([^)]*\))?', payload)
            if m_d:
                delay = m_d.group(0)
                payload = (payload[:m_d.start()] + payload[m_d.end():]).strip()

            # Salle : pivot strict sur la DERNIÈRE occurrence
            strict_hits = list(_STRICT_ROOM.finditer(payload))
            if strict_hits:
                last_pivot = strict_hits[-1]
                pre = payload[:last_pivot.start()].rstrip()
                m_art = re.search(r'(?:La|Le|Les|Au[x]?|De|Du|L\')\s*$', pre, re.IGNORECASE)
                pivot_start = m_art.start() if m_art else last_pivot.start()
                title_block = payload[:pivot_start].strip(" -—(,:")
                details_raw = payload[pivot_start:]
                year_hits = list(re.finditer(_YEAR_RE, details_raw, re.IGNORECASE))
                if year_hits:
                    last_y = year_hits[-1]
                    year = last_y.group(0).strip()
                    details_raw = (details_raw[:last_y.start()] + " " + details_raw[last_y.end():]).strip()
                room_cand, _ = _split_details(details_raw)
                # Si _split_details retourne un mot seul ambigu, tenter sur la string complète
                room = _normalize_room(details_raw if len(room_cand.split()) <= 1 else room_cand)
            else:
                title_block = payload
                year_hits = list(re.finditer(_YEAR_RE, payload, re.IGNORECASE))
                if year_hits:
                    year = year_hits[-1].group(0).strip()

            # Extrait "- Année: X" et "- Salle: X" inline dans le titre
            m_annee = re.search(r'\s*[-–]\s*[Aa]nn[ée]e?\s*:\s*([^\-–]+?)(?=\s*[-–]|$)', title_block)
            if m_annee:
                if not year:
                    year_raw = m_annee.group(1).strip()
                    # Tronquer après le token ordinal (évite de capturer la matière qui suit)
                    _ord = re.match(r'(?:toutes?\s+les?\s*)?(?:\d+|[IVX]+)\s*[eèêéè][mre][eé]?', year_raw, re.IGNORECASE)
                    year = _ord.group(0).strip() if _ord else year_raw
                title_block = (title_block[:m_annee.start()] + title_block[m_annee.end():]).strip(" -—")
            m_salle_inl = re.search(r'\s*[-–]\s*[Ss]alle\s*:\s*(.+?)(?=\s*[-–]|$)', title_block)
            if m_salle_inl:
                if not room: room = _normalize_room(m_salle_inl.group(1).strip())
                title_block = (title_block[:m_salle_inl.start()] + title_block[m_salle_inl.end():]).strip(" -—")

            # Retire l'écho de matière en fin de titre (ex: "… Cheminée toilettes) Divers")
            for label, _ in _SUBJECTS:
                m_echo = re.search(rf'\b{re.escape(label)}\s*$', title_block, re.IGNORECASE)
                if m_echo:
                    trimmed = title_block[:m_echo.start()].strip()
                    if len(trimmed) > 5:
                        subject = _normalize_subject(label)
                        title_block = trimmed
                    break
            else:
                # Tentative fuzzy : les derniers 1-3 mots via trigram (tolère les typos OCR)
                words = title_block.split()
                for n in (3, 2, 1):
                    if len(words) > n:
                        cand = ' '.join(words[-n:])
                        norm = _normalize_subject(cand)
                        if norm and norm.lower() != cand.lower():
                            trimmed = ' '.join(words[:-n]).strip(' -—,')
                            if len(trimmed) > 5:
                                if not subject: subject = norm
                                title_block = trimmed
                            break

            # Retire le "Cours " initial redondant avec "ANNONCE DE COURS"
            title_block = re.sub(r'^[Cc]ours\s+', '', title_block).strip()
            # Retire la préposition initiale résiduelle (ex: "de Sort…" → "Sort…")
            title_block = re.sub(r'^(?:de|du|d\'|des|en|la|le|les|au[x]?)\s+', '', title_block, flags=re.IGNORECASE)

            # "[Matière] : [Titre du cours]" (format colon — 1er screenshot)
            m_col = re.match(r'^([^:]{1,40}):\s*(.+)$', title_block, re.DOTALL)
            if m_col:
                potential = m_col.group(1).strip()
                norm = _normalize_subject(potential)
                if not subject and norm != potential:
                    subject = norm
                    message = m_col.group(2).strip()
                else:
                    message = title_block
            else:
                message = title_block

            # "[Matière] - [Titre du cours]" (format tiret — ex: "Divers - Les Mangas Golmu")
            # Si le sujet est déjà connu et que le message commence par "Sujet - ", retire ce préfixe
            if subject:
                m_dash = re.match(
                    r'^' + re.escape(subject) + r'\s*[-–]\s*(.+)$',
                    message, re.IGNORECASE | re.DOTALL
                )
                if m_dash:
                    message = m_dash.group(1).strip()
                else:
                    # Essai via _normalize_subject sur la partie avant " - "
                    m_dash2 = re.match(r'^([^–\-]{1,40})\s*[-–]\s*(.+)$', message, re.DOTALL)
                    if m_dash2:
                        norm2 = _normalize_subject(m_dash2.group(1).strip())
                        if norm2 and norm2.lower() == subject.lower():
                            message = m_dash2.group(2).strip()

        # Fallback : année extraite de l'en-tête (avant "ANNONCE DE COURS")
        if not year and _year_from_header:
            year = _year_from_header

        # Nettoie les fuites OCR dans le message :
        # Résidu de nom d'auteur en début : token ALL-CAPS avec ponctuation (ex: "STERIJ,VG Potion")
        message = re.sub(r'^[A-ZÀ-Ü][A-Z,\.;\-]{2,}\S*\s+', '', message)
        # Résidu "initiale + Nom propre" en début (ex: "R Greenshadow Club…" → "Club…")
        message = re.sub(r'^[A-ZÀ-Ü]\s+[A-ZÀ-Ü][a-zà-ü]{2,}\s*', '', message)
        # Artefacts OCR : lettres minuscules isolées (émojis mal lus → "g", "s"…)
        message = re.sub(r'^(?:hdm|hmd)\s+', '', message, flags=re.IGNORECASE)  # retire abréviation matière OCR
        message = re.sub(r'^[A-ZÀ-Ü][A-Z,\.;\-]{2,}\S*\s+', '', message)  # résidu ALL-CAPS avec ponctuation
        message = re.sub(r'^[A-ZÀ-Ü]\s+[A-ZÀ-Ü][a-zà-ü]{2,}\s+', '', message)  # initiale + Nom propre
        message = re.sub(r'^[a-z]\s+', '', message)          # en début : "g Alchimie" → "Alchimie"
        message = re.sub(r'^[a-z]\s+', '', message)          # artefact OCR : minuscule isolée début (émojis mal lus)
        message = re.sub(r'\s+[a-z]\s+', ' ', message)   # au milieu : "Cervorns g X" → "Cervorns X"
        message = re.sub(r'\s+[a-z]$', '', message)          # en fin minuscule
        message = re.sub(r'\s+[A-Z](?:\s+[A-Z]\.?)?$', '', message)          # en fin majuscule isolée ou initiale + majuscule (ex: "Sat F.")
        # Retire les résidus d'année qui ont fui dans le message (ex: "X Eme Année" / "5ème Année")
        message = re.sub(_YEAR_RE, '', message, flags=re.IGNORECASE).strip(' -—,')
        # Retire les suffixes ordinaux orphelins en fin de message (ex: "Cours 2 Eme" → "Cours 2")
        # "2 Eme" vient de "2ème année" dont "année" était dans la section icône et non dans le titre
        message = re.sub(r'\s+[eèêé]m[eé]?\s*$', '', message, flags=re.IGNORECASE).strip(' -—,')
        # Nettoie les doubles virgules laissées par le retrait de l'année (ex: ", , En" → ", En")
        message = re.sub(r',\s*,+', ',', message)
        # Retire les prépositions isolées en fin de message (ex: "Sort (Luridium), En" → "Sort (Luridium)")
        # Ajouter avant le nettoyage final : retrait de la salle si elle fuit dans le message
        message = re.sub(rf'\s*/\s*{re.escape(room)}.*$', '', message, flags=re.IGNORECASE) if room else message
        # Retrait des artefacts OCR : espaces/chiffres orphelins
        message = re.sub(r'\s+\d\s+\d(?=\s|$)', '', message)
        message = re.sub(r'(?:,\s*)?(?:en|de|du|au[x]?|la|le|les|sur|par)\s*$', '', message, flags=re.IGNORECASE)
        message = re.sub(r'\s{2,}', ' ', message).strip(' ,;-—')

        # Rejette faux positifs OCR
        if len(author) < 3 or len(message) < 4:
            return None

        ann: dict = {"type": "cours", "author": author, "message": message}
        if delay:   ann["delay"]   = delay
        if year:    ann["year"]    = year
        if room:    ann["room"]    = _normalize_room(room)
        if subject: ann["subject"] = _normalize_subject(subject)
        return ann

    # ══════════════════════════════════════════════════════════════════════════
    else:
        # Format FiveM : "[NOM ANNONCEUR] ANNONCE DE [CORPS DU MESSAGE]"
        # → auteur = AVANT "ANNONCE DE", message = APRÈS
        m = re.search(r'ANNONCE\s+DE\s+(?!COURS\b)', joined, re.IGNORECASE)
        if not m:
            return None

        # Auteur : derniers tokens capitalisés avant "ANNONCE DE"
        pre = joined[:m.start()].strip()
        _GEN_TOK = r'[A-ZÀ-Ü][A-ZÀ-Üa-zà-ü\-]+'
        m_a = re.search(
            rf'({_GEN_TOK}(?:\s+{_GEN_TOK}){{0,2}})\s*$',
            pre
        )
        author = m_a.group(1).strip() if m_a else ""

        # Message : tout ce qui suit "ANNONCE DE"
        raw_after = joined[m.end():].strip()

        # Format alternatif : "ANNONCE DE [PRÉNOM NOM] [MESSAGE]"
        # Tous les persos ont exactement prénom + nom → on prend toujours 2 tokens capitalisés
        if not author:
            m_a2 = re.match(rf'^({_GEN_TOK}\s+{_GEN_TOK})\s+', raw_after)
            if m_a2:
                author = m_a2.group(1).strip()
                raw_after = raw_after[m_a2.end():].strip()

        message = raw_after.strip(" -—,[]")

        if not message:
            return None
        return {"type": "general", "author": author, "message": message}

def ann_hash(ann: dict) -> str:
    # Hash sur type+auteur uniquement pour absorber les variations OCR du message
    return hashlib.md5(f"{ann['type']}:{ann.get('author','').lower().strip()}".encode()).hexdigest()

# ── Token ─────────────────────────────────────────────────────────────────────
def load_token() -> str | None:
    try:
        if TOKEN_FILE.exists():
            return json.loads(TOKEN_FILE.read_text()).get("exeToken")
    except Exception: pass
    return None

def save_token(exe_token: str):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"exeToken": exe_token}))

# ── API ───────────────────────────────────────────────────────────────────────
def send_heartbeat(tok: str) -> dict:
    try:
        r = requests.post(API_HEARTBEAT, json={"exeToken": tok, "version": VERSION}, timeout=15)
        if not (r.ok or r.status_code == 200):
            return {"_err": f"HTTP {r.status_code}"}
        return r.json()
    except Exception as e:
        return {"_err": str(e)}

def _pil_to_b64(pil_img: Image.Image) -> str:
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

def send_announcement(tok: str, ann: dict, screenshot_b64: str | None = None, ocr_log: str | None = None, on_log=None) -> bool:
    try:
        payload: dict = {"exeToken": tok, "announcement": ann}
        if screenshot_b64:
            payload["screenshot"] = screenshot_b64
        if ocr_log:
            payload["ocr_log"] = ocr_log
        r = requests.post(API_ANNOUNCE, json=payload, timeout=30)
        if on_log: on_log(f"Announce réponse ({r.status_code}): {r.text[:120]}")
        return r.ok
    except Exception as e:
        if on_log: on_log(f"Announce erreur: {e}")
        return False

def link_token(one_time: str) -> str | None:
    try:
        r = requests.post(API_LINK, json={"token": one_time}, timeout=10)
        if r.ok: return r.json().get("exeToken")
    except Exception: pass
    return None

# ── Démarrage Windows ────────────────────────────────────────────────────────
STARTUP_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "CourSW"

def is_startup_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as k:
            winreg.QueryValueEx(k, STARTUP_NAME)
        return True
    except Exception:
        return False

def set_startup(enabled: bool):
    import winreg
    exe_path = str(Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve())
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try: winreg.DeleteValue(k, STARTUP_NAME)
            except FileNotFoundError: pass

# ── Vérification GitHub releases au démarrage ────────────────────────────────
GITHUB_RELEASES_URL = "https://api.github.com/repos/paolito13/CoursSW/releases/latest"

def check_github_update(on_log, on_notify=None):
    """Vérifie si une nouvelle version est disponible sur GitHub et met à jour si besoin."""
    try:
        on_log("🔍 Vérification des mises à jour…")
        r = requests.get(GITHUB_RELEASES_URL, timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        if not r.ok:
            on_log(f"⚠️  GitHub releases inaccessible ({r.status_code})")
            return
        data = r.json()
        latest = data.get("tag_name", "").lstrip("v")
        if not latest:
            return
        def _ver(s):
            try: return tuple(int(x) for x in s.split("."))
            except: return (0,)
        if _ver(latest) <= _ver(VERSION):
            on_log(f"✅ Version à jour ({VERSION})")
            return
        on_log(f"🆕 Nouvelle version {latest} disponible (actuelle : {VERSION})")
        assets = data.get("assets", [])
        zip_asset = next((a for a in assets if a["name"].endswith(".zip")), None)
        if not zip_asset:
            on_log("⚠️  Aucun fichier ZIP trouvé dans la release")
            return
        _do_self_update(zip_asset["browser_download_url"], on_log, on_notify)
    except Exception as e:
        on_log(f"⚠️  Erreur vérification GitHub : {e}")

# ── Worker ────────────────────────────────────────────────────────────────────
def _do_self_update(download_url: str, on_log, on_notify=None):
    """
    Mise à jour --onedir : télécharge le ZIP, extrait dans un dossier sibling,
    un BAT remplace le dossier courant et relance l'exe.
    """
    import zipfile, shutil
    def _notify(title: str, msg: str):
        if on_notify:
            try: on_notify(title, msg)
            except Exception: pass
    zip_path = None
    new_dir  = None
    bat_path = None
    try:
        exe_path    = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve()
        install_dir = exe_path.parent          # …/CourSW/
        parent_dir  = install_dir.parent       # …/  (là où le BAT vivra)
        zip_path    = parent_dir / "CourSW_update.zip"
        new_dir     = parent_dir / "CourSW_new"
        old_dir     = parent_dir / "CourSW_old"
        bat_path    = parent_dir / "update.bat"

        on_log(f"📁 Dossier install : {install_dir}")
        on_log(f"📁 Dossier parent  : {parent_dir}")
        on_log("⬇️  Téléchargement de la mise à jour…")
        _notify("🔄 Mise à jour CourSW", "Téléchargement en cours…")

        for attempt in range(1, 4):
            try:
                with requests.get(download_url, stream=True, timeout=180) as r:
                    r.raise_for_status()
                    expected = int(r.headers.get('Content-Length', 0))
                    received = 0
                    with open(zip_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            f.write(chunk)
                            received += len(chunk)
                if expected and received < expected:
                    raise ValueError(f"Téléchargement incomplet ({received}/{expected} octets)")
                break
            except Exception as e:
                on_log(f"⚠️  Tentative {attempt}/3 échouée : {e}")
                if attempt == 3:
                    raise
                time.sleep(5)

        if zip_path.stat().st_size < 1_000_000:
            raise ValueError(f"ZIP trop petit ({zip_path.stat().st_size} o) — probablement bloqué par l'antivirus")

        on_log("📦 Extraction…")
        if new_dir.exists():
            shutil.rmtree(new_dir)
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(new_dir)

        # Le ZIP contient un sous-dossier CourSW/
        inner = new_dir / "CourSW"
        extracted = inner if inner.is_dir() else new_dir

        bat = (
            '@echo off\n'
            f'cd /d "{parent_dir}"\n'
            'timeout /t 6 /nobreak > nul\n'
            f'if exist "{old_dir}" rmdir /s /q "{old_dir}"\n'
            f'rename "{install_dir}" "CourSW_old"\n'
            f'move /y "{extracted}" "{install_dir}"\n'
            f'if exist "{old_dir}" rmdir /s /q "{old_dir}"\n'
            f'rmdir /s /q "{new_dir}" 2>nul\n'
            f'del "{zip_path}" 2>nul\n'
            f'start "" "{exe_path}"\n'
            'del "%~f0"\n'
        )
        bat_path.write_text(bat, encoding='utf-8')

        on_log("✅ Mise à jour prête — redémarrage dans 3s…")
        _notify("✅ CourSW mis à jour", "Redémarrage automatique…")
        time.sleep(3)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        os._exit(0)
    except Exception as e:
        on_log(f"⚠️  Mise à jour échouée : {e}")
        _notify("⚠️ Mise à jour échouée", str(e)[:80])
        for p in [p for p in [zip_path, bat_path] if p]:
            try: p.unlink()
            except Exception: pass
        if new_dir and new_dir.exists():
            try: __import__('shutil').rmtree(new_dir)
            except Exception: pass
            except Exception: pass


class Worker(threading.Thread):
    def __init__(self, exe_token: str, on_status, on_log, on_notify=None):
        super().__init__(daemon=True)
        self.tok = exe_token
        self.on_status = on_status
        self.on_log = on_log
        self.on_notify = on_notify
        self.running = True
        self.seen: dict[str, float] = {}

    def stop(self): self.running = False

    def _heartbeat_loop(self):
        """Thread dédié au heartbeat — indépendant de l'OCR."""
        # Heartbeat immédiat
        try:
            hb = send_heartbeat(self.tok)
            self.on_log(f"Heartbeat initial : {hb}")
        except Exception as e:
            self.on_log(f"❌ Erreur heartbeat initial : {e}")
            hb = {}

        if hb.get("update_required"):
            self.on_status("🔄 Mise à jour requise…")
            self.on_log("⚠️  Nouvelle version requise — mise à jour automatique…")
            dl = hb.get("download_url", "")
            if dl:
                threading.Thread(target=_do_self_update, args=(dl, self.on_log, self.on_notify), daemon=True).start()
            self.running = False
            return

        if not hb.get("ok"):
            self.on_log("⚠️  Heartbeat refusé — token invalide ou site inaccessible")
        self.on_status("🟢 Connecté — surveillance active" if hb.get("ok") else "🔴 Impossible de joindre le site")
        if not _BROWSER_FLAG_FILE.exists():
            _BROWSER_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _BROWSER_FLAG_FILE.touch()
            webbrowser.open(SITE_URL)

        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self.running:
                break
            try:
                hb = send_heartbeat(self.tok)
                if hb.get("update_required"):
                    self.on_status("🔄 Mise à jour requise…")
                    self.on_log("⚠️  Nouvelle version requise — mise à jour automatique…")
                    dl = hb.get("download_url", "")
                    if dl:
                        threading.Thread(target=_do_self_update, args=(dl, self.on_log, self.on_notify), daemon=True).start()
                    self.running = False
                    return
                if hb.get("_err"):
                    self.on_log(f"⚠️ Heartbeat échoué : {hb['_err']}")
                elif not hb.get("ok"):
                    self.on_log(f"⚠️ Heartbeat refusé : {hb}")
                else:
                    self.on_log("💓 Heartbeat ok")
                self.on_status("🟢 Connecté — surveillance active" if hb.get("ok") else "🔴 Impossible de joindre le site")
            except Exception as e:
                self.on_log(f"⚠️  Heartbeat erreur : {e}")

    def _update_check_loop(self):
        """Vérifie les mises à jour GitHub toutes les 10 min pendant que l'exe tourne."""
        time.sleep(600)
        while self.running:
            check_github_update(self.on_log, self.on_notify)
            time.sleep(600)

    def run(self):
        self.on_log("Démarrage de la surveillance…")
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._update_check_loop, daemon=True).start()
        # Attend que le heartbeat initial soit traité
        time.sleep(2)

        while self.running:
            try:
                now = time.time()

                win = find_fivem_window()
                if not win:
                    time.sleep(5)
                    continue

                hwnd, rect = win
                # Ne capture que si FiveM est la fenêtre au premier plan
                # (évite de capturer VS Code ou d'autres fenêtres derrière FiveM)
                fg = win32gui.GetForegroundWindow()
                if fg != hwnd:
                    time.sleep(2)
                    continue

                # Capture initiale rapide
                pil  = capture_region(rect)
                text = ocr_image(pil)
                best_pil, best_text = pil, text

                # Multi-capture uniquement si une annonce semble présente (évite de ralentir le cycle normal)
                if text.strip() and re.search(r'ANNONCE\s+DE\s+COURS', text, re.IGNORECASE):
                    captures: list[tuple[Image.Image, str]] = [(pil, text)]
                    for _ in range(2):
                        time.sleep(0.5)
                        try:
                            pil_i = capture_region(rect)
                            txt_i = ocr_image(pil_i)
                            captures.append((pil_i, txt_i))
                        except Exception:
                            pass

                    def _ocr_score(txt: str) -> int:
                        a = parse_announcement(txt) if txt.strip() else None
                        if a is None: return -1
                        s = 0
                        if a.get('subject'): s += 10
                        if a.get('room'):    s += 10
                        if a.get('year'):    s += 10
                        if a.get('delay'):   s += 10
                        s += min(len(a.get('message', '')), 80)
                        return s

                    best_pil, best_text = max(captures, key=lambda c: _ocr_score(c[1]))

                text = best_text
                ann  = parse_announcement(text) if text.strip() else None

                if ann:
                    self.on_log(f"[OCR✅] {' '.join(text.split())[:120]}")

                if ann:
                    h = ann_hash(ann)
                    seen_ago = now - self.seen.get(h, 0)
                    if seen_ago > 300:
                        self.seen[h] = now
                        try:
                            scr_b64 = _pil_to_b64(best_pil)
                        except Exception:
                            scr_b64 = None
                        ok = send_announcement(self.tok, ann, screenshot_b64=scr_b64, ocr_log=text, on_log=self.on_log)
                        label = "cours" if ann["type"] == "cours" else "générique"
                        self.on_log(
                            f"{'✅' if ok else '⚠️'} Annonce {label} "
                            f"({ann.get('author','?')}) : {ann.get('message','')[:45]}…"
                        )
                    else:
                        self.on_log(f"⏳ Cooldown (seen il y a {int(seen_ago)}s) — {ann.get('author','?')}")

                self.seen = {k: v for k, v in self.seen.items() if now - v < 600}

            except Exception as e:
                self.on_log(f"❌ Erreur : {type(e).__name__}: {e}")

            time.sleep(CAPTURE_INTERVAL)

# ── GUI ───────────────────────────────────────────────────────────────────────
BG   = "#0d1a1e"
BG2  = "#0a1215"
GOLD = "#fde090"
BLUE = "#90c8ff"
GRN  = "#5de89e"

class LinkDialog(tk.Toplevel):
    def __init__(self, parent, on_success):
        super().__init__(parent)
        self.on_success = on_success
        self.title("Liaison du compte Discord")
        self.geometry("460x290")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.grab_set()

        tk.Label(self, text="🔑 Lier ton compte Discord",
                 font=("Segoe UI", 13, "bold"), bg=BG, fg=GOLD).pack(pady=(22, 6))
        tk.Label(self,
                 text="1. Va sur le site → onglet 📡 Cours → clique sur\n"
                      "   « Générer mon code de liaison »\n"
                      "2. Colle le code ci-dessous et valide.",
                 font=("Segoe UI", 9), bg=BG, fg="#6b8a9a", justify="center").pack(pady=(0, 14))

        row = tk.Frame(self, bg=BG)
        row.pack(padx=30, fill="x")
        tk.Label(row, text="Code :", bg=BG, fg="#b0c8d0", font=("Segoe UI", 9)).pack(side="left")
        self.entry = tk.Entry(row, font=("Consolas", 9), bg=BG2,
                              fg=GOLD, insertbackground=GOLD, relief="flat", bd=4, width=36)
        self.entry.pack(side="left", padx=(8, 0), fill="x", expand=True)

        self.msg = tk.StringVar()
        tk.Label(self, textvariable=self.msg, font=("Segoe UI", 9), bg=BG, fg="#e05050").pack(pady=6)

        tk.Button(self, text="✅  Valider", command=self._validate,
                  bg="#1e4a2e", fg=GRN, relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=18, pady=6).pack(pady=4)
        tk.Button(self, text="🌐  Ouvrir le site",
                  command=lambda: webbrowser.open(SITE_URL),
                  bg=BG, fg="#5080a0", relief="flat", font=("Segoe UI", 8)).pack()

    def _validate(self):
        code = self.entry.get().strip()
        if not code:
            self.msg.set("Entre un code de liaison."); return
        self.msg.set("Validation en cours…")
        self.update()
        tok = link_token(code)
        if tok:
            save_token(tok)
            self.on_success(tok)
            self.destroy()
        else:
            self.msg.set("❌ Code invalide ou expiré. Génères-en un nouveau sur le site.")


def _make_tray_icon() -> Image.Image:
    """Génère une icône simple pour le system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill="#1a3a4a")
    d.ellipse([18, 18, 46, 46], fill="#5de89e")
    return img


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CourSW — Seven Wands")
        self.geometry("520x420")
        self.resizable(False, False)
        self.configure(bg=BG)

        # Cache dans la barre des tâches — visible seulement dans le tray
        self.withdraw()
        self.overrideredirect(False)

        self.worker: Worker | None = None
        self._build_ui()
        self._setup_tray()

        # Active le démarrage automatique à la première installation
        if not is_startup_enabled():
            try:
                set_startup(True)
                self._startup_var.set(True)
            except Exception:
                pass

        # Vérification GitHub au démarrage (avant de lancer le worker)
        threading.Thread(
            target=check_github_update,
            args=(lambda m: self.after(0, self._log, m), self._notify),
            daemon=True
        ).start()

        tok = load_token()
        if tok:
            self._start(tok)
        else:
            self._show_window()
            self._ask_link()

    def _build_ui(self):
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=20, pady=(18, 0))
        tk.Label(hdr, text="📡 CourSW", font=("Segoe UI", 17, "bold"), bg=BG, fg=GOLD).pack(side="left")
        tk.Label(hdr, text="  Seven Wands — Observateur de cours",
                 font=("Segoe UI", 9), bg=BG, fg="#4a6a7a").pack(side="left")
        self.ver_var = tk.StringVar(value=f"v{VERSION}")
        tk.Label(hdr, textvariable=self.ver_var,
                 font=("Segoe UI", 8), bg=BG, fg="#3a5a6a").pack(side="right")

        self.status_var = tk.StringVar(value="⏳ Démarrage…")
        tk.Label(self, textvariable=self.status_var, font=("Segoe UI", 10),
                 bg=BG, fg=GRN).pack(pady=(14, 0))

        tk.Frame(self, bg="#1a2e38", height=1).pack(fill="x", padx=20, pady=8)

        lf = tk.Frame(self, bg=BG2, bd=0)
        lf.pack(fill="both", expand=True, padx=20)
        self.log_box = tk.Text(lf, bg=BG2, fg="#b0c8d0", font=("Consolas", 9),
                               state="disabled", wrap="word", bd=0, padx=8, pady=8)
        sc = ttk.Scrollbar(lf, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True)

        bf = tk.Frame(self, bg=BG)
        bf.pack(fill="x", padx=20, pady=12)
        tk.Button(bf, text="🔗  Changer de compte", command=self._ask_link,
                  bg="#1a2e38", fg=BLUE, relief="flat", font=("Segoe UI", 9), padx=10, pady=5
                  ).pack(side="left")
        tk.Button(bf, text="🌐  Ouvrir le site",
                  command=lambda: webbrowser.open(SITE_URL),
                  bg="#1a2e38", fg=BLUE, relief="flat", font=("Segoe UI", 9), padx=10, pady=5
                  ).pack(side="left", padx=(8, 0))
        tk.Button(bf, text="✕  Réduire",
                  command=self._hide_window,
                  bg="#1a2e38", fg="#6b8a9a", relief="flat", font=("Segoe UI", 9), padx=10, pady=5
                  ).pack(side="right")

        # Toggle démarrage automatique
        sf = tk.Frame(self, bg=BG)
        sf.pack(fill="x", padx=20, pady=(0, 14))
        self._startup_var = tk.BooleanVar(value=is_startup_enabled())
        tk.Checkbutton(sf, text="🚀  Lancer au démarrage de Windows",
                       variable=self._startup_var, command=self._toggle_startup,
                       bg=BG, fg="#b0c8d0", selectcolor=BG2,
                       activebackground=BG, font=("Segoe UI", 9), bd=0
                       ).pack(side="left")

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("📡 CourSW — Seven Wands", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ouvrir", self._show_window, default=True),
            pystray.MenuItem("Ouvrir le site", lambda: webbrowser.open(SITE_URL)),
            pystray.MenuItem("Changer de compte", lambda: self.after(0, self._ask_link)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", self._quit),
        )
        self.tray = pystray.Icon("CourSW", _make_tray_icon(), "CourSW — Seven Wands", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _notify(self, title: str, msg: str):
        try: self.tray.notify(msg, title)
        except Exception: pass

    def _show_window(self, *_):
        self.after(0, self.deiconify)
        self.after(0, self.lift)

    def _hide_window(self):
        self.withdraw()

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, msg: str):
        self.status_var.set(msg)
        self.tray.title = f"CourSW — {msg}"

    def _start(self, tok: str):
        if self.worker:
            self.worker.stop()
        self.worker = Worker(
            tok,
            on_status=lambda m: self.after(0, self._set_status, m),
            on_log=lambda m: self.after(0, self._log, m),
            on_notify=self._notify,
        )
        self.worker.start()

    def _toggle_startup(self):
        try:
            set_startup(self._startup_var.get())
            state = "activé" if self._startup_var.get() else "désactivé"
            self._log(f"🚀 Démarrage automatique {state}")
        except Exception as e:
            self._log(f"⚠️  Impossible de modifier le démarrage : {e}")
            self._startup_var.set(is_startup_enabled())

    def _ask_link(self):
        self._show_window()
        LinkDialog(self, on_success=self._start)

    def _quit(self):
        if self.worker: self.worker.stop()
        self.tray.stop()
        self.destroy()

    def destroy(self):
        if self.worker: self.worker.stop()
        try: self.tray.stop()
        except Exception: pass
        try: _BROWSER_FLAG_FILE.unlink(missing_ok=True)
        except Exception: pass
        super().destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app._hide_window)  # croix = réduire dans le tray
    app.mainloop()


if __name__ == "__main__":
    main()
