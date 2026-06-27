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
VERSION = "1.5.175"
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


_mss_instance = None

def capture_region(rect):
    global _mss_instance
    if _mss_instance is None:
        _mss_instance = mss.mss()
    wx, wy, wx2, wy2 = rect
    ww, wh = wx2 - wx, wy2 - wy
    region = {
        "left":   wx,
        "top":    wy,
        "width":  int(ww * CAP_RIGHT),
        "height": int(wh * CAP_BOTTOM),
    }
    try:
        shot = _mss_instance.grab(region)
    except Exception:
        # Recréer l'instance si elle est corrompue
        _mss_instance = mss.mss()
        shot = _mss_instance.grab(region)
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

def _lev(a: str, b: str) -> int:
    """Distance d'édition (Levenshtein) — meilleure que les trigrammes pour une
    déformation OCR d'1-2 lettres au milieu d'un mot (ex: 'magicuje' vs 'magiques')."""
    m, n = len(a), len(b)
    d = list(range(n + 1))
    for i in range(1, m + 1):
        prev, d[0] = d[0], i
        for j in range(1, n + 1):
            cur = d[j]
            d[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, d[j], d[j - 1])
            prev = cur
    return d[n]

def _lev_ratio(a: str, b: str) -> float:
    """Distance d'édition normalisée [0..1] : 0 = identiques, ~1 = totalement différents."""
    m = max(len(a), len(b))
    return _lev(a, b) / m if m else 0.0

def _best_canonical(raw: str, table: list[tuple[str, list[str]]], min_sim: float = 0.0) -> str:
    """
    Étape 1 : correspondance par mots-clés.
    Étape 2 : similarité trigrammes sur le label normalisé.
    Si min_sim > 0 et qu'aucun mot-clé n'a matché et que la meilleure similarité
    trigramme est sous le seuil, retourne "" (pas de correspondance fiable)
    au lieu de forcer un label canonique arbitraire.
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
    if min_sim > 0 and best_sim < min_sim:
        return ""
    return best_label


# Salles officielles + leurs variantes OCR / abréviations
_ROOMS: list[tuple[str, list[str]]] = [
    ('La Cabane',                  ['cabane', 'dans']),
    ('Salle CMS',                  ['cms']),   # le jeu affiche "Salle CMS" (≠ Salle Potions)
    ('Salle Potions',                  ['potion', 'potions', 'salle de potion', 'potions ouvert']),
    ('Salle Créatures Magiques',   ['creature', 'creatur', 'magique', 'magiques', 'salle creature', 'magiwes', 'magiqye', 'magic&jues', 'magic&jues', 'creatures magic', 'maciqye', 'maciqyes', 'maciqje', 'macqje', 'macawe', 'macawes', 'magi(uje', 'magiqje', 'mac,jqje', 'macte', 'macaques', 'cabysside', 'terragor']),
    ('Serre 1',                    ['serre 1', 'serre1', 'serre', 'serrfs']),
    ('Serre 2',                    ['serre 2', 'serre2', 'serre fongique']),
    ('Serre 3',                    ['serre 3', 'serre3']),
    ('Serre 4',                    ['serre 4', 'serre4']),
    ('Salle DCFM (toilettes)',     ['dcfm', 'ocfm', 'toilette', 'saile', 'sox', 'soxis', 'morte', 'mortevsen', 'boianiqjje', 'botaniq', 'macte', 'dcfm (toilettes)']),
    ('Salle Musique',              ['musique', 'musiqye', 'inscripitoon', 'inscri']),
    ('Salle Généraliste',          ['generaliste', 'general', 'generalist', 'generauste', 'generau', 'generaliete', 'classe generaliste', 'classe general', 'sat f general', '11 x club', '11x', 'x club', 'duel league', 'duel en groupe', 'capture de zone', 'saile generausie', 'saile generau', 'generausie', 'salle generauste', 'dans generauste', 'club serre', 'saile generausie dans', 'potions', 'potions serre', 'serre 1', 'eme annee', 'annee annonce', 'au balai', 'balai', 'club de duel', 'club pour', 'club salle', 'musique club']),
    ('Salle Potions',              ['salle potion', 'salle potions', 'potion', 'potions']),
    ('Salle de Duel',              ['duel', 'tolte', 'tour', 'tou-u-r', 'saue', 'musiqye', 'ft-1palto', 'ft-1palt', 'duel pour', 'lorica', 'lorica g', 'g', 'voltumb', 'voltumbfua', 'dans', 'dans5', 'cheminee', 'cheminée', 'tour uastronomie', 'de duel']),
    ('Salle de Littérature',       ['litter', 'littera', 'litterature', 'litteratur', 'literature', 'litteratur', 'fiqa', 'informis', 'divers']),
    ("Salle d'Étude de Golmue",    ['golmue', 'golmu', 'golmve', 'etude de golm', 'study', 'golmus', 'sai e', 'sai', 'generaliste', 'etude', 'oivers', 'divers']),
]

def _normalize_room(raw: str) -> str:
    if not raw:
        return raw
    # Serre avec numéro : détection directe prioritaire
    m = re.search(r'serre\s*(\d)', _deaccent(raw))
    if m:
        return f'Serre {m.group(1)}'
    # min_sim : une salle tronquée à un mot générique ("SALLE", "SERRE") sans
    # qualificatif distinctif ne doit PAS être forcée vers une salle au hasard
    # (ex: "SALLE" → "Salle de Duel"). Mieux vaut aucune salle qu'une fausse.
    return _best_canonical(raw, _ROOMS, min_sim=0.42)


# Matières officielles + leurs variantes OCR / abréviations
_SUBJECTS: list[tuple[str, list[str]]] = [
    ('Alchimie - Botanique', ['alchimie', 'botanique', 'alch', 'alchimie-botanique']),
    ('Sorts',                ['sort', 'sorts', 'magie', 'sai', 'soris']),
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

def _normalize_subject_strict(raw: str) -> str:
    """Comme _normalize_subject, mais retourne "" si aucun mot-clé ne matche et
    que la similarité trigramme est trop faible — pour ne pas confondre la fin
    d'un titre de cours avec une matière au hasard (ex: "L'Austrel", "Des Brumes")."""
    if not raw:
        return raw
    return _best_canonical(raw, _SUBJECTS, min_sim=0.42)

def _subject_has_keyword(text: str, label: str) -> bool:
    """Vrai si `text` contient un mot-clé OFFICIEL de la matière `label` (match par
    sous-chaîne, comme _best_canonical). Sert à distinguer un vrai écho de matière
    ("Créature Magicuje" contient le mot-clé 'creature') d'une simple ressemblance
    trompeuse ("Émotions" ne contient AUCUN mot-clé de Potions, il ne matchait que
    par trigramme)."""
    key = _deaccent(text).lower()
    for lab, kws in _SUBJECTS:
        if lab == label:
            return any(kw in key for kw in kws)
    return False


# Mots qui signalent la fin du nom d'auteur
_STOP = (
    r'[Cc]ours|[Tt]outes?|[Tt]ous|[Ll]es?|[Dd]ans|[Aa]ux?|[Dd]es?'
    r'|[Uu]ne?|[Ee]n|[Ss]a[lr][le]|[Ss]erre|[Pp]our|[Aa]vec|[Dd]e\b'
    # Matières / mots-clés FiveM fréquents après le nom
    r'|[Ss]orts?|[Pp]otions?|[Dd]ivers|[Cc]lubs?|[Hh][Dd][Mm]|[Aa]lchimie'
    r'|[Bb]otanique|[Aa]stronomie|[Tt]ransfiguration|[Mm][ée]tamorphose'
    r'|[Dd][ée]fense|[Dd]ivination|[Aa]rithmancie|[Ss]oins'
    r'|[Cc]r[eé]ature|[Mm]agique|[Cc]ours|[Hh]istoire|[Ll]itt[eé]rature|[Ll]eague\b'
    r'|[Dd]ernier|[Rr]appel|[Cc]ommence|[Dd][eé]bute|[Aa]nnonce|[Uu]rgent'
    r'|[Cc]\'[Ee]st|[Cc][Ee]st'
    r'|[Ff]action|[Ee]quipe|[Éé]quipe|[Gg]roupe|[Gg]uilde|[Cc]lan'
    r'|[Ll]a\b|[Ss]aut\b|[Cc]orrespondance|[Nn]umérolog|[Ii]nterpretation|[Ii]nterprétation'
    r'|[Cc]omplot|[Nn]yxie|[Ii]nitiation|[Bb][aâ]timent|[Cc]ouloir|[Mm]onter'
    # Types de potions (jamais un nom d'annonceur) + mots de début de titre fréquents
    r'|[Nn]anis|[Mm]agna|[Ff]orte|[Mm]axima|[Pp]arva|[Mm]ixtura|[Aa]mplificatio|[Tt]onique|[Ee]lixir|[Ii]nfusion|[Pp]raticue|[Pp]ratique'
    r'|[Cc]r[eé]ation|[Rr]attrapage|[Rr][eé]union|[Cc]ercle|[Tt]h[eé]orie'
    # Salles (évite que "Duel" soit capturé comme nom)
    r'|[Dd]uel\b|[Gg]eneraliste|[Gg]énéraliste|[Gg]eneralust'
    # Tokens d'années (VII, EME, ERE, ANNEE) qui saignent dans l'auteur
    r'|\b(?:VII|VI|V|IV|III|II)\s*[Ee]me\b|[Vv]ii\b|[Ee]me\b|[Éé]me\b|[Ee]re\b|[Éé]re\b|[Aa]nn[eé]e\b|secatr[a-z]*|[Aa]u\b|zito|[Cc][Ll][Ii][Nn][Tt][Aa][Ll][Ii][Ee][Nn]|SILAS|BENNETT|(?:^|\s)(?:[0-9]+\s*)?(?:[eèêé]m[eé]|[eèé]r[eé])\s+ann[eé]e'
    # Tokens OCR parasites tout-caps en début d'auteur (STERIJ, BARJNOV, LENFIEZ.D, etc.)
    r'|(?:[A-ZÀ-Ü]{2,}[A-ZÀ-Ü0-9]*\.?(?![a-zà-ü]))|[Vv][Oo][Nn]\b|[Bb]ataille\b|[Ll][Oo][Nn][Ww][Ee][Aa][Cc][Xx]'
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


def _smart_title(text: str) -> str:
    """Title-case mot à mot un texte OCR tout-majuscules (auteur/message des annonces
    génériques), en préservant la ponctuation autour et les apostrophes/tirets internes.
    Ne touche pas les mots déjà en casse mixte (l'OCR a su lire la casse)."""
    def _cap(w: str) -> str:
        if len(w) <= 1:
            return w
        m = re.fullmatch(r'([^0-9A-Za-zÀ-ÿ]*)([0-9A-Za-zÀ-ÿ\'’\-]+)([^0-9A-Za-zÀ-ÿ]*)', w)
        if not m:
            return w
        pre, core, post = m.group(1), m.group(2), m.group(3)
        if not core.isupper():          # déjà en casse mixte → on garde
            return w
        for sep in ("'", "’", "-"):
            if sep in core:
                core = sep.join(p.capitalize() if p.isupper() else p for p in core.split(sep))
                break
        else:
            core = core.capitalize()
        return pre + core + post
    return ' '.join(_cap(w) for w in text.split())


# Caractères du français courant (lettres + accents + ponctuation usuelle). Un token FINAL qui
# contient autre chose (ò, ø, æ, •, runes d'emoji…) est un emoji/symbole mal lu par l'OCR.
_FR_CHARS = "a-zA-Z0-9àâäéèêëîïôöùûüÿçœæÀÂÄÉÈÊËÎÏÔÖÙÛÜŸÇŒÆ"
_FR_PUNCT = r".,;:!?'’\"()«»\-/&%°…\s"

def _strip_emoji_garble(message: str) -> str:
    """Retire en FIN de message le bruit d'emoji/symbole mal lu (ex: 😊 → 'üò-ee', '•œ').
    Ne touche jamais le texte français accentué ni un garble qui n'est pas en fin."""
    if not message:
        return message
    message = re.sub(rf'(?:\s+\S*[^{_FR_CHARS}{_FR_PUNCT}]\S*)+\s*$', '', message)
    message = re.sub(r'\s*[•·]+\s*$', '', message)  # puce orpheline en fin
    return message.strip(' -—,.')


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
    # Parenthèse fermante ")" au MILIEU d'un mot = artefact OCR (ex: "CALEIX)R" → "CALEIXR",
    # "ASHWCX)D" → "ASHWCXD") — un vrai ")" est en fin de mot, jamais entre deux lettres.
    # Évite que le nom d'auteur soit tronqué au ")" (ex: "Caleix" au lieu de "Caledor Mériastrel").
    joined = re.sub(r'(?<=[A-Za-zÀ-ÿ])\)(?=[A-Za-zÀ-ÿ])', '', joined)
    # Virgule SANS espace coincée entre deux MAJUSCULES = "/" mal lu (séparateur de liste
    # de sorts en petites capitales, ex: "DEFENDO/FLIPALTO,TIBOBO"). Une vraie virgule
    # française est toujours suivie d'une espace → ce motif est forcément un garble.
    # Recolle le token tout-majuscules au lieu de le laisser se faire title-caser isolément.
    joined = re.sub(r'(?<=[A-ZÀ-Ü]),(?=[A-ZÀ-Ü])', '/', joined)
    # Corrections typos OCR fréquentes sur les noms de salles et mots-clés
    joined = re.sub(r'\bGENERAUSTE\b', 'GÉNÉRALISTE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bGENERALUSTE\b', 'GÉNÉRALISTE', joined, flags=re.IGNORECASE)
    # Variantes OCR de MAGIQUE/MAGIQUES : MAC,JQJE / MAC'Q!JE.S / MACAWES / MAGI(UJE / MAGIQJE / MACIQJE / MACQJE
    joined = re.sub(r'\bMAGIWES\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGIQYE\b', 'MAGIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGIQJE[S]?\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGI\(UJE[S]?\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMAGIWES\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r"\bMAC[',\.\!\s]?[QJ][!\.\s]?[JU][E]?[\.S]?\b", 'MAGIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMACAWES\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMACIQJE[S]?\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMACQJE[S]?\b', 'MAGIQUES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMACtE\b', 'MAGIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bHISIOIRES?\b', 'HISTOIRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bHISIOIRES?\b', 'HISTOIRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bHIST0IRES?\b', 'HISTOIRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r"HISI['’]OIRES", 'HISTOIRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMUSQUE\b', 'MUSIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLITTERATURE\b', 'LITTÉRATURE', joined, flags=re.IGNORECASE)
    # "LI-rrÉRATURE" / "LIrrÉRATURE" = "Littérature" mal lu ("TT" → "-rr"/"rr"). Ce garble
    # n'apparaît qu'en lecture casse-mixte → on rend "Littérature" (et non LITTÉRATURE) pour
    # ne pas créer un îlot tout-majuscules dans un message déjà en Title-Case.
    joined = re.sub(r'\bLI[-\s]?rr[ÉE]RATURE\b', 'Littérature', joined, flags=re.IGNORECASE)
    # Variantes OCR de SALLE : SAUE / SAIE / SAILE / SAI F / SAT F / SAT F- / SAT F. / SA' 'F / SATJF / SAI 1 Fr / SAI.IE / SALIE
    joined = re.sub(r"SALLE O'ETUDE", "SALLE D'ETUDE", joined, flags=re.IGNORECASE)
    joined = re.sub(r"SAI\s+1\s+E(?=D')", 'SALLE ', joined, flags=re.IGNORECASE)
    joined = re.sub(r"ED'ETUDEDEGOLMUE", "D'ETUDE DE GOLMUE", joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAUE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAIE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAILE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSALIE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAI\s+F\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAT\s*F[-\.]?\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r"\bSA['\s]+['\s]+F\b", 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSATJF\s*:', 'SALLE:', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAI\s+1\s*Fr?\.?\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAT\s+1\s*F\.?\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAI\s*[\.]\s*IE\b', 'SALLE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSATIF\b', 'SALLE', joined, flags=re.IGNORECASE)  # SATIF: → SALLE
    joined = re.sub(r'\bSERREI\b', 'SERRE I', joined, flags=re.IGNORECASE)  # SERREI → SERRE I = SERRE 1
    joined = re.sub(r'\bBOTANIQFI\'?\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSALLECMS\b', 'SALLE CMS', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSERRFS\b', 'SERRES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMUSIQYE\b', 'MUSIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBIBLIOTH[EÉ]QJE\b', 'BIBLIOTHÈQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBIBLIOTH[EÉ]QYE\b', 'BIBLIOTHÈQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bOOLMUE\b', 'GOLMUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bGOLM[VY]S\b', 'GOLMUS', joined, flags=re.IGNORECASE)  # "Sports Golmvs" (U lu V)
    joined = re.sub(r'\bETUDEDE\b', 'ETUDE DE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLrr+f?RATURE\b', 'LITTÉRATURE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAUSPOTI\w*\b', 'SALLE POTIONS', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bÉLÈVFS\b', 'ÉLÈVES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bCIN[OQ][UY]I[EÈ]ME\b', 'CINQUIÈME', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBOTANIQSJE\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBOIANIQYE\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBOTANIQVE\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBO[VT]ANIQ[VU]E\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)  # BOVANIQVE (T→V, U→V)
    joined = re.sub(r'\bBOTANIQYE\b', 'BOTANIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMONOE\b', 'MONDE', joined, flags=re.IGNORECASE)  # "Création du Monoe" (D lu O) — cours récurrent
    # "Fonkdateur/Fonrdateur" = "Fondateur" (le Fondateur Vert) avec une lettre parasite insérée
    # par l'OCR. On RETIRE juste la lettre en trop → la casse environnante est préservée (pas
    # d'îlot tout-MAJ si le texte est déjà en casse mixte).
    joined = re.sub(r'\b(FON)[KR](DATEUR)\b', r'\1\2', joined, flags=re.IGNORECASE)
    # "1 FS" = "LES" mal lu (L→1, e→F) — uniquement devant "élèves" (annonce du Fondateur Vert :
    # "attend les élèves"). Ancré pour éviter tout faux positif.
    joined = re.sub(r'\b1\s+FS\b(?=\s+[ÉE]L?[èeéEÈÉ]VES?)', 'LES', joined, flags=re.IGNORECASE)
    # "Cabysside/Cabyssioe" = lieu "L'Abysside" mal lu : l'OCR fusionne "L'A" en "Ca" (et parfois
    # D→O). Lieu récurrent des cours Créature Magique. Sortie tout-MAJ → le site title-case en
    # "L'Abysside" (titleIfUpper capitalise après l'apostrophe).
    joined = re.sub(r"\bCABYSSI[DO]E\b", "L'ABYSSIDE", joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bTHÉORIWE\b', 'THÉORIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bTHÃORIQVE\b', 'THÉORIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bM[ÉEÃ]ORIQ[_\s]?[YVU]E\b', 'THÉORIQUE', joined, flags=re.IGNORECASE)  # "mÉORIQ_YE" (T→m)
    joined = re.sub(r'\bPOIIONS\b', 'POTIONS', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bPOIiONS\b', 'POTIONS', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bv0TlO[,\.]?[Vv][Ss]?\b', 'POTIONS', joined, flags=re.IGNORECASE)  # "v0TlO,VS" → POTIONS
    joined = re.sub(r'\bPOIION\b', 'POTION', joined, flags=re.IGNORECASE)
    # OCR D→O fréquent quand l'annonce est lue tout en MAJUSCULES :
    #   "OE" isolé = "DE", "OUEL" = "DUEL" (ex: "SALLE OE OUEL" → "SALLE DE DUEL")
    joined = re.sub(r'\bOE\b', 'DE', joined)
    joined = re.sub(r'\bOUEL\b', 'DUEL', joined, flags=re.IGNORECASE)
    # "SORIS" = catégorie "SORTS" mal lue (écho de la matière dans le corps)
    joined = re.sub(r'\bSORIS\b', 'SORTS', joined, flags=re.IGNORECASE)
    # "SOURREN" = "SOUTIEN" mal lu ("TI" → "rr" en petites capitales serif) — récurrent
    # dans les titres "Club de Soutien". "sourren" n'est pas un mot → correction sûre.
    joined = re.sub(r'\bSOU?RR[EÉ]N\b', 'SOUTIEN', joined, flags=re.IGNORECASE)
    # "i-ùVES" / "i-uVES" = "ÉLÈVES" (même famille de garble que ÉLÈVFS ci-dessus)
    joined = re.sub(r'\bi-[ùu]VES\b', 'ÉLÈVES', joined, flags=re.IGNORECASE)
    # Variantes OCR de DANS (déclencheur du délai) : OANS/0ANS (D→O/0), DAN5/DANJ (S→5/J),
    # DAMS (N→M) — uniquement quand suivi d'un nombre, pour éviter les faux positifs
    joined = re.sub(r'\b[D0O]A[NM][S5J]\b(?=\s+\d)', 'DANS', joined, flags=re.IGNORECASE)
    # Variantes OCR de MINUTE(S) : MINUTEtS) / MINUTECS) / MINUJE(S) / Minutecs
    joined = re.sub(r'\bMINUTEtS\)', 'MINUTE(S)', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMINUTECS\)', 'MINUTE(S)', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMinutecs\)', 'Minutes', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMINUJE\(S\)', 'MINUTE(S)', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bMINUTE[A-Za-z]\(S\)', 'MINUTE(S)', joined, flags=re.IGNORECASE)
    # Mot "minute(s)" garblé après "DANS X" (Minuit, Minues, Mjnute, Mini, Minuit(s)…) →
    # après "DANS <nombre>" l'unité est TOUJOURS minute(s), donc on normalise.
    joined = re.sub(r'\b(DANS\s+\d+\s+)M[IJ]N\w*(?:\([sS]\))?', r'\1MINUTE(S)', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLIWIDES\b', 'LIQUIDES', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLIWIDES(?=[A-ZÀÈÙÉ])', 'LIQUIDES ', joined, flags=re.IGNORECASE)  # mot fusionné (ex: LIWIDESMAGIQYES)
    # Format fusionné "(SALLE:XXXX)" → "SALLE XXXX" pour que _norm_tok puisse normaliser
    joined = re.sub(r'\(SALLE\s*:\s*([A-ZÀ-Üa-zà-ü\s]+?)\)', r'SALLE \1', joined, flags=re.IGNORECASE)
    # "ENFANTINE" comme alias d'année (cours pour 1ère-2ème)
    joined = re.sub(r'\bENFANTINE\b', '1ère ANNÉE', joined, flags=re.IGNORECASE)
    # "111" avant ANNÉE = chiffre romain "III" (3ème année) mal lu — cohérent avec "11"=II=2ème
    # ci-dessous. L'OCR perd souvent le "ÈME" : "III ÈME ANNÉE" → "111 ANNÉE" → "3ème année".
    joined = re.sub(r'\b111\s*(?:[èeéÈ]me?\s*)?(?=ANN[EÉÈ])', '3ème ', joined, flags=re.IGNORECASE)
    # "CLUB-" ou "CLUB :" en préfixe de titre = activité parascolaire, pas une anomalie → retirer.
    # MAIS pas quand "CLUB" suit "DE/DU" (ex: "COURS DE CLUB - COLLABORATION") : là "Club"
    # est la matière du cours, pas un préfixe parasite → on le garde.
    joined = re.sub(r'(?<!de\s)(?<!du\s)\bCLUB\s*[-:]\s*', '', joined, flags=re.IGNORECASE)
    # Overlays FiveM résiduels : tokens GPU% / CPU% collés (ex: "650/6 GPU: 66%")
    joined = re.sub(r'\b\d+[/%]\d*\s*(?=GPU|CPU)', '', joined, flags=re.IGNORECASE)
    # Résidus artefacts OCR FiveM header (fiveM@ by Cfx.re…, "ps" ou "fps" isolés en nombre)
    joined = re.sub(r'\bfiveM@[^A-ZÀ-Ü]*(?=ANNONCE)', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\b\d+\s*(?:fps|ps)\b', '', joined, flags=re.IGNORECASE)
    # Compteur FPS avec chiffres mal lus (ex: "100fps" → "Iêêfps", "Ioofps") : tout token
    # finissant par "fps" est l'overlay FiveM, jamais un mot/nom réel → supprimer.
    joined = re.sub(r'\b\w*fps\b', '', joined, flags=re.IGNORECASE)
    # Overlay "FPS: 237" / "FPS:- 237" / "FPS - 237" (label AVANT le nombre, avec ponctuation)
    # — sinon le FPS se colle à "ERE ANNÉE" et devient une fausse année ("237 ERE ANNÉE")
    joined = re.sub(r'\bFPS\s*[:\-]*\s*\d+', '', joined, flags=re.IGNORECASE)
    # "Ping 15ms" sans deux-points (overlay réseau) → supprimer
    joined = re.sub(r'\bPing\s*[:\-]*\s*\d+\s*ms\b', '', joined, flags=re.IGNORECASE)
    # Token overlay CPU mal lu et isolé (ex: "CPI" pour "CPU:") au milieu du texte
    joined = re.sub(r'\bCP[IU]\b(?!\s*:)', '', joined, flags=re.IGNORECASE)
    # PRATIQYE → PRATIQUE (OCR Y→U), idem PRATIQVE
    joined = re.sub(r'\bPRATIQ[YV]E\b', 'PRATIQUE', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bPRATIC[&UV]JE\b', 'PRATIQUE', joined, flags=re.IGNORECASE)  # "Praticuje"/"Pratic&je"
    joined = re.sub(r'\bTH[EÉ]ORIC[&UV]JE\b', 'THÉORIQUE', joined, flags=re.IGNORECASE)  # "Théoric&je"
    joined = re.sub(r'\bMUSIC[&UV]JE\b', 'MUSIQUE', joined, flags=re.IGNORECASE)  # "Musicuje"
    # Artefact "cv.URs" (OCR de l'emoji cours) avant PAR
    joined = re.sub(r'\bcv\.URs?\b', '', joined, flags=re.IGNORECASE)
    # Caractères parasites OCR (bullet •, point médian ·)
    joined = re.sub(r'[•·]', '', joined)
    # Overlays réseau : "Ping: 15ms", "7.170 HDM" (stat FPS avec séparateur milliers)
    joined = re.sub(r'\bPing\s*:\s*\d+\s*ms\b', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\b\d{1,3}\.\d{3}\b', '', joined)
    joined = re.sub(r'\b(?:HDM|HMD)\b', '', joined)  # abréviation timer FiveM (reste après suppression du nombre)
    # Timestamp heure + compteur inscrits du popup FiveM (ex: "04:39 5/24 inscrits")
    joined = re.sub(r'\b\d{1,2}:\d{2}\b', '', joined)
    joined = re.sub(r'\b\d+/\d+\s*inscrits?\b', '', joined, flags=re.IGNORECASE)
    # Année de la colonne gauche du popup ("11 TOUTES ANNÉES", "Xème ANNÉE") : elle
    # apparaît AVANT "ANNONCE DE COURS" et serait perdue par le strip ci-dessous.
    # On la capture maintenant pour la réinjecter en fallback plus loin.
    _presrip_year_hits = list(re.finditer(_YEAR_RE, joined, re.IGNORECASE))
    _presrip_header_year = _presrip_year_hits[0].group(0).strip() if _presrip_year_hits else ""
    # Header "fiveM@ by Cfx.re - Sevenwands FA - Le seul et l'unique Xs" → strip tout avant ANNONCE
    joined = re.sub(r'^.*?(?=ANNONCE\s+DE\s+COURS)', '', joined, flags=re.IGNORECASE | re.DOTALL)
    # Pollution Alt-Tab / capture d'écran : si le screenshot a été pris avec le sélecteur de
    # fenêtres (Alt-Tab) ou l'Outil Capture ouvert, l'OCR avale des TITRES DE FENÊTRES Windows
    # ("FiveM® by Cfx.re - Sevenwands…", "Outil Capture d'écran", "Discord… LATE"). Aucun de ces
    # libellés n'apparaît dans un vrai cours → on coupe tout à partir du 1er marqueur de fenêtre.
    joined = re.sub(
        r"\s*[»>]?\s*(?:five\s?m\b|cfx\.re|sevenwands|outil\s+(?:de\s+)?capture|capture\s+d'écran|discord)\b.*$",
        '', joined, flags=re.IGNORECASE | re.DOTALL)
    # Deux annonces empilées dans le même screenshot (ex: "… IMMÉDIATEMENT ANNONCE DE COURS PAR …")
    # → on ne garde que la PREMIÈRE pour ne pas mélanger les deux cours dans un seul message.
    _anns = list(re.finditer(r'ANNONCE\s+DE\s+COURS', joined, re.IGNORECASE))
    if len(_anns) >= 2:
        joined = joined[:_anns[1].start()].strip()
    # "Xs" timer en secondes avant ANNONCE (ex: "29 s ANNONCE") → déjà géré par le strip ci-dessus
    # Format DIVERS(COURS DE X) → extraire X comme titre
    joined = re.sub(r'\bDIVERS\s*\(COURS\s+DE\s+([^)]+)\)', r'\1', joined, flags=re.IGNORECASE)
    # "PL : CPU: XX/Y GPU: XX/Y" (overlay FiveM) -> supprimer
    joined = re.sub(r'\bPL\s*:\s*CPU:\s*[\d/%]+\s*GPU:\s*[\d/%]+', '', joined, flags=re.IGNORECASE)
    # Bouton fermer FiveM "X" isole avant DANS -> supprimer
    joined = re.sub(r'\s+X\s+(?=DANS\b)', ' ', joined, flags=re.IGNORECASE)
    # Chiffres orphelins résiduels (overlay OCR "11" mal lu, fragments comme "11 X" en fin d'icône)
    # Aussi "11 ANNÉE" (lido "II ème année" en "11 ANNÉE") avant "DANS"
    # Aussi "11" isolé en fin de message (overlay corrompu, ex: "Lecmseti.Fsvampires 11")
    joined = re.sub(r'\b11(?:\s+(?:X|ANNÉE|ann[eé]e))?\s+(?=DANS\b|$)', ' ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\s+11\s*$', ' ', joined)
    # Chiffres orphelins parasites avant DANS (reliquat d'overlay OCR : "rSHDM - Elizabeth Bath 111 X ... DANS")
    joined = re.sub(r'\s+\d{2,}\s+(?:X\s+)?(?=DANS\b)', ' ', joined, flags=re.IGNORECASE)
    # Section icône FiveM "g [MATIÈRE] SALLE [SALLE] [X]" (juste avant DANS/IMMÉDIATEMENT) :
    # c'est l'étiquette du jeu elle-même → source LA PLUS FIABLE pour matière + salle.
    # On la CAPTURE avant de la stripper, pour ne pas avoir à deviner depuis le corps
    # (sinon un mot du corps comme "Émotions" se fait confondre avec une matière "Potions").
    _icon_subject_raw, _icon_room_raw = "", ""
    _m_icon_cap = re.search(
        r'\sg\s+(.+?)(?=\s+DANS\b|\s+IMM[EÉ]DIATEMENT\b|$)',
        joined, flags=re.IGNORECASE | re.DOTALL
    )
    if _m_icon_cap:
        _seg = _m_icon_cap.group(1).strip()
        # La salle de l'icône commence à "SALLE" OU "SERRE" (les deux types de lieux).
        _m_sr = re.search(r'(.*?)\b(SA[LI]LE|SERRE)\b\s*(.*)', _seg, flags=re.IGNORECASE | re.DOTALL)
        if _m_sr:
            _icon_subject_raw = _m_sr.group(1)
            _icon_room_raw = _m_sr.group(2) + ' ' + _m_sr.group(3)
        else:
            _icon_subject_raw = _seg
        # Nettoie les artefacts (croix de fermeture "X", "11", chiffres orphelins)
        _icon_subject_raw = re.sub(r'\b(?:X|11|\d+)\b', ' ', _icon_subject_raw).strip()
        _icon_room_raw = re.sub(r'\bX\b\s*$', '', _icon_room_raw).strip()
    # Formes canoniques de l'icône (matière/salle) — autoritaires si reconnues
    _icon_subject = _normalize_subject_strict(_icon_subject_raw) if _icon_subject_raw else ""
    _icon_room = _normalize_room(_icon_room_raw) if _icon_room_raw else ""
    # Titre happé dans l'icône AVANT la matière (cas des titres entourés d'emojis 🎵 lus "g X",
    # ex: "g X Club - Musique Club SALLE Musique" → segment matière = "Club - Musique Club" =
    # [titre "Club - Musique"] + [matière "Club"]). Si le segment contient un tiret et finit par
    # un mot qui EST la matière, on isole le titre — récupéré comme message si le corps est vide.
    _icon_title_raw = ""
    if _icon_subject and _icon_subject_raw:
        _toks = _icon_subject_raw.split()
        # On retire en fin le(s) token(s) qui redonnent la matière ; ce qui reste devant est
        # un titre SEULEMENT s'il ne redonne pas lui-même la matière (ex: "Musique Club" →
        # titre "Musique" + matière "Club" ; "Créature Magique" → tout est matière, pas de titre).
        for _k in range(1, len(_toks)):
            _tail = ' '.join(_toks[-_k:])
            _head = ' '.join(_toks[:-_k])
            if _normalize_subject_strict(_tail).lower() == _icon_subject.lower() and _head and \
               _normalize_subject_strict(_head).lower() != _icon_subject.lower():
                _icon_title_raw = _head
                break
    # Icone "g" FiveM suivi des donnees popup (categorie + salle + X) avant DANS -> supprimer
    joined = re.sub(r'\sg\s+.+?(?=DANS\b)', ' ', joined, flags=re.IGNORECASE | re.DOTALL)
    # Variante sans "DANS" : la section icône se termine par IMMÉDIATEMENT ou la fin de chaîne
    # (sinon "g CRÉATURE MAGICUJE SALLE …" se recopiait en fin de message). On consomme aussi
    # le terminateur IMMÉDIATEMENT (le délai est détecté séparément sur le texte d'origine).
    joined = re.sub(r'\sg\s+.+?\bIMM[EÉ]DIATEMENT\b', ' ', joined, flags=re.IGNORECASE | re.DOTALL)
    joined = re.sub(r'\sg\s+\S.*$', '', joined, flags=re.IGNORECASE | re.DOTALL)
    joined = re.sub(r'\bIMM[EÉ]DIATEMENT\b', ' ', joined, flags=re.IGNORECASE)
    # Compteur inscrits sans barre (ex: "0130 iNSCiits") -> supprimer
    joined = re.sub(r'\b\d{3,4}\s+i[Nn]sc[Ii]i?ts?\b', '', joined, flags=re.IGNORECASE)
    # Délai écrit DANS la phrase (ex: "… en salle du CMS dans 4 minutes sur Minotaure …") :
    # un "DANS X MINUTES" suivi de "SUR/POUR" n'est pas le délai-délimiteur du popup mais une
    # partie de la phrase → on le retire pour ne pas couper le vrai contenu à la troncature.
    joined = re.sub(r'\bDANS\s+\d+\s+MINUTES?(?:\(S\))?\b(?=\s+(?:SUR|POUR)\b)', '', joined, flags=re.IGNORECASE)
    # Tronquer apres DANS X MINUTE(S): supprime bas popup FiveM + 2eme annonce visible
    m_delay_full = re.search(r'DANS\s+\d+\s+MINUTES?(?:\(S\))?', joined, re.IGNORECASE)
    # (le délai est capturé plus loin dans la branche cours via m_delay_full — pas ici : la
    # variable `delay` n'existe pas encore à ce stade)
    joined = re.sub(r'(DANS\s+\d+\s+MINUTES?(?:\(S\))?(?:\s*\([^)]*\))?)\b.*', r'\1', joined, flags=re.IGNORECASE | re.DOTALL)
    # Artefact OCR d'emoji lu "ft" en début de token (ex: ftBOBO, ft-1PALTO → supprimés entièrement)
    # Couvre "ft" suivi de majuscules embarquées (ftBOBO) ET "ft-" avec tiret
    joined = re.sub(r'\bft(?:-)?\S+', '', joined, flags=re.IGNORECASE)
    # OCR fusionne parfois "NOM,PRENOM" avec une virgule (ex: "CLI,WALLEN" → "CLI WALLEN")
    joined = re.sub(r'\b([A-ZÀ-Ü]{2,}),([A-ZÀ-Ü])', r'\1 \2', joined)
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
    # OCR fusionne "/ 3" en "13" (I+digit sans espace) → on restaure l'ordinal.
    # [2-9] uniquement (pas "11" = II = 2ème, géré par la règle ci-dessous).
    # IGNORECASE : "12E ANNÉE" en majuscules → "2E ANNÉE" = 2ème.
    joined = re.sub(r'\b1([2-9]\s*(?:[eèê]me?|[eè]re?|e)\b)', r'\1', joined, flags=re.IGNORECASE)
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
    # Fallback : année captée dans l'en-tête avant qu'il ne soit strippé (cf. _presrip_header_year)
    if not _year_from_header and _presrip_header_year:
        _year_from_header = _presrip_header_year
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
            # Apostrophe : normalise chaque partie (D'OPHIDREL → D'Ophidrel)
            if "'" in w or '’' in w:
                sep = "'" if "'" in w else '’'
                parts = w.split(sep, 1)
                return sep.join(_norm_tok(p) if p else p for p in parts)
            # Parenthèse/point : split et normalise chaque fragment (ÉTOILÉE(ALCHIMIE → Étoilée(Alchimie)
            m_fused = re.match(r'^([A-ZÀ-Üa-zà-ü\-]{2,})([\(\)\.\,\:])([A-ZÀ-Ü]{1,}.*)$', w)
            if m_fused:
                return _norm_tok(m_fused.group(1)) + m_fused.group(2) + _norm_tok(m_fused.group(3))
            if '.' in w:
                parts = w.split('.')
                def _cap_part(p: str) -> str:
                    if not p:
                        return p
                    # Gère les parties avec ponctuation autour ex: "(FLIPALTO)" ou "THÉORIQUE)"
                    m2 = re.fullmatch(r'([^A-ZÀ-Üa-zà-ü]*)([A-ZÀ-Üa-zà-ü\-]{2,})([^A-ZÀ-Üa-zà-ü]*)', p)
                    if m2 and m2.group(2).isupper():
                        return m2.group(1) + m2.group(2).capitalize() + m2.group(3)
                    return p
                return '.'.join(_cap_part(p) for p in parts)
            # Normalise aussi les mots entre ponctuation ex: (FLAMETTE) → (Flamette)
            m = re.fullmatch(r'([^A-ZÀ-Üa-zà-ü]*)([A-ZÀ-Üa-zà-ü\-]{2,})([^A-ZÀ-Üa-zà-ü]*)', w)
            if m and m.group(2).isupper():
                return m.group(1) + m.group(2).capitalize() + m.group(3)
            return w
        _raw_payload_tokens = payload.split()  # avant normalisation (casse brute conservée)
        payload = ' '.join(_norm_tok(w) for w in payload.split())

        # ── Auteur ────────────────────────────────────────────────────────────
        # Token nom : mot commençant par majuscule (Dupont) OU initiale seule (L / L.)
        # S'arrête aux abbréviations tout-caps (HDM, HMD…) et aux mots _STOP
        _NAME_TOK = r'(?:[A-ZÀ-Ü][A-ZÀ-Üa-zà-ü\'\-]+|[A-ZÀ-Ü]\.?(?=\s|$))'
        # Contractions tout-caps (C'EST, D'UNE…) → jamais un nom propre
        # Contraction/élision (C'EST, D'UNE, L'Histoire…) → jamais un nom propre. Couvre le
        # tout-majuscules ET le Title-Case (le payload est déjà en casse mixte à ce stade,
        # donc "C'Est" doit être reconnu comme stop, pas capturé dans l'auteur).
        _ALL_CAPS_CONTRACTION = r"[A-ZÀ-Ü]['’][A-ZÀ-Üa-zà-ü]{2,}"
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
            # Retire un stop word en tête d'auteur (ex: "Duel League" → "League", puis trop court → rejeté)
            author = re.sub(rf'^(?:{_STOP})\s+', '', author).strip()
            # Retire un suffixe parasite de type ".D" ou ".X" en fin de nom (OCR artefact)
            author = re.sub(r'\.[A-ZÀ-Ü]$', '', author).strip()
            # Retire les suffixes numériques parasites (ex: "Paolito 7*10" → "Paolito")
            author = re.sub(r'\s+[\d\*\+\/\-\.]+\s*\S*$', '', author).strip()
            # Mot du TITRE absorbé par erreur en fin d'auteur : l'auteur est en petites capitales
            # (l'OCR rend des minuscules, ex: "CALELOk MÉRIASTRfL") tandis que le titre est en
            # grandes capitales pures ("LOCUS MINOR"). Si le DERNIER token de l'auteur était
            # tout-majuscules dans le brut alors qu'un token précédent contient une minuscule,
            # c'est le 1er mot du titre → on le rend au message (l'auteur ramené à 2 mots est
            # recalé par le référentiel serveur). Ne se déclenche pas si l'auteur est entièrement
            # en capitales (aucune minuscule avant) → pas de régression sur les noms tout-caps.
            _overflow_tok = ""
            _auth_toks = author.split()
            if len(_auth_toks) >= 3:
                _idx0 = len(payload[:m_a.start(1)].split())
                _auth_raw = _raw_payload_tokens[_idx0:_idx0 + len(_auth_toks)]
                if len(_auth_raw) == len(_auth_toks) and \
                   re.fullmatch(r"[A-ZÀ-Ü][A-ZÀ-Ü'’\-]+", _auth_raw[-1]) and \
                   any(re.search(r'[a-zà-ÿ]', t) for t in _auth_raw[:-1]):
                    _overflow_tok = _auth_toks[-1]
                    author = ' '.join(_auth_toks[:-1])
            # Validation auteur : rejette les noms ALL-CAPS avec tiret interne (ex: "Mu-IER", "Oreg-L")
            # Un nom valide a au moins une lettre minuscule (après normalisation Title Case)
            # On rejette aussi les noms de 1 seul mot trop courts (< 3 chars)
            _author_words = author.split()
            if _author_words:
                # Vérifie si le nom ressemble à un artefact OCR ALL-CAPS avec tiret
                # Ex: "Mu-IER" → contient tiret + séquence ALL-CAPS après
                _has_allcaps_hyphen = any(
                    re.search(r'[A-ZÀ-Ü]-[A-ZÀ-Ü]{2,}', w) for w in _author_words
                )
                # Ex: "MYERS" reste ALL-CAPS après _norm_tok (si l'OCR n'a pas reconnu les minuscules)
                _all_upper = all(w.isupper() and len(w) >= 2 for w in _author_words if len(w) > 1)
                if _has_allcaps_hyphen or (_all_upper and len(_author_words) <= 2 and sum(len(w) for w in _author_words) < 8):
                    author = ""
            payload = payload[m_a.end():].strip()
            if _overflow_tok:
                payload = (_overflow_tok + ' ' + payload).strip()
            # Résidu OCR d'un nom de famille cassé : auteur tronqué à une initiale seule
            # (ex: "Klaus M") + fragment minuscule en tête de payload (ex: "yns" pour
            # "Myers"). Le payload est en Title-Case, donc un token entièrement minuscule
            # en tête est forcément un artefact OCR. On le RETIRE (sans le recoller : un
            # fragment OCR cassé "yns" donnerait un faux nom "Myns"). L'auteur reste à
            # l'initiale propre "Klaus M" — l'annuaire serveur la complétera en "Klaus
            # Myers" par match de préfixe sur l'historique des auteurs connus.
            if re.search(r'\s[A-ZÀ-Ü]$', author):
                payload = re.sub(r'^[a-zà-üœæ]{2,5}\b\s+', '', payload)
            # Retire les caractères non-alpha en début de payload (ex: ".A Hdm…" → "Hdm…")
            payload = re.sub(r'^[^a-zA-ZÀ-ÿ(]+', '', payload)

        # ── Séparation description / détails ──────────────────────────────────
        message = ""
        year    = ""
        delay   = ""
        room    = ""
        # La matière de l'icône (étiquette du jeu) est fiable : on la fixe DÈS LE DÉPART
        # pour que le retrait du préfixe "[Matière] - …" du message s'appuie dessus
        # (ex: "Créature Magique - Le Spectrevif" → "Le Spectrevif").
        subject = _icon_subject

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
            title_raw = re.sub(r'^(?:dans|de|du|d\'|des|en|la|le|les|au[x]?|pour|sort|pratiq[a-z_]*|nants?)\s+', '', title_raw, flags=re.IGNORECASE)
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
            elif not delay and m_delay_full:
                # Fallback : délai capturé avant stripping de la section icône
                delay = m_delay_full.group(0)

            # Salle : pivot strict sur la DERNIÈRE occurrence du corps — MAIS seulement si
            # l'icône n'a pas déjà donné la salle. Sinon un "salle" interne à la phrase
            # ("… attendus EN SALLE DE Créature Magique POUR UN COURS SUR …") couperait le
            # message à tort (la vraie salle vient de l'icône, le corps est la phrase complète).
            strict_hits = list(_STRICT_ROOM.finditer(payload))
            if strict_hits and not _icon_room:
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
                # Seuil strict requis : sinon la fin du titre du cours (ex: "L'Austrel",
                # "Des Brumes") se fait quasi systématiquement confondre avec une matière.
                words = title_block.split()
                for n in (3, 2, 1):
                    if len(words) > n:
                        cand = ' '.join(words[-n:])
                        norm = _normalize_subject_strict(cand)
                        # Un mot-clé isolé dans une fenêtre de 2-3 mots peut matcher même
                        # quand le reste de la fenêtre est du vrai contenu de titre (ex:
                        # "Magique : L'Austrel" contient "magique" mais n'est pas un écho de
                        # matière) — on exige donc aussi une similarité globale candidat↔label.
                        # Vrai écho si : trigramme fort (≥0.6) OU bien le candidat contient un
                        # MOT-CLÉ officiel de la matière ET en est très proche (édition ≤0.4).
                        # La 2e voie attrape les échos très garblés ("Créature Magicuje"≈
                        # "Créatures Magiques", édition 0.22, mot-clé 'creature') ; le mot-clé
                        # évite le faux positif "Émotions"→"Potions" (aucun mot-clé, juste un
                        # trigramme trompeur), et "Sortilège…"→"Sorts" reste écarté (édition 0.76).
                        if norm and norm.lower() != cand.lower() and (
                               _trigram_sim(_deaccent(cand), _deaccent(norm)) >= 0.6 or
                               (_subject_has_keyword(cand, norm) and
                                _lev_ratio(_deaccent(cand).lower(), _deaccent(norm).lower()) <= 0.4)):
                            trimmed = ' '.join(words[:-n]).strip(' -—,')
                            if len(trimmed) > 5:
                                if not subject: subject = norm
                                title_block = trimmed
                            break

            # Retire le "Cours " initial redondant avec "ANNONCE DE COURS"
            title_block = re.sub(r'^[Cc]ours\s+', '', title_block).strip()
            # Retire la préposition initiale résiduelle (ex: "de Sort…" → "Sort…").
            # On NE retire PAS les articles le/la/les : ils font presque toujours partie du
            # vrai titre ("Le Fangor", "La Biche Des Brumes", "Les Thérianthropes", "Les Mangas").
            title_block = re.sub(r'^(?:de|du|d\'|des|en|au[x]?|pour)\s+', '', title_block, flags=re.IGNORECASE)

            # "[Matière] : [Titre du cours]" (format colon — 1er screenshot)
            # Garde-fou : un VRAI préfixe de matière ne contient ni " - " ni un marqueur de
            # salle ; si la partie avant le ":" en contient un, le ":" est parasite (vient d'un
            # "Salle :"/"Sai F :"/"CA' \ F :" mal lu) et on ne doit PAS couper avant le titre
            # (ex: "Sort (Les Flammettes…) Salle : Littérature", "Le Fangor - CA' \ F : …").
            m_col = re.match(r'^([^:]{1,40}):\s*(.+)$', title_block, re.DOTALL)
            if m_col and ' - ' not in m_col.group(1) and '–' not in m_col.group(1) and \
               not re.search(r'\b(?:salle|serre|sai|sat|sau|cms|dcfm)\b', m_col.group(1), re.IGNORECASE):
                potential = m_col.group(1).strip()
                norm = _normalize_subject(potential)
                if not subject and norm != potential:
                    subject = norm
                    message = m_col.group(2).strip()
                elif subject and norm and norm.lower() == subject.lower():
                    # Le préfixe "[Matière] :" répète la matière déjà extraite ailleurs
                    # (ex: "Potion : Nanis d'Humécume - Pratique" avec subject déjà = Potions)
                    # → on retire seulement le préfixe, sans manger le titre du cours
                    message = m_col.group(2).strip()
                else:
                    message = title_block
            else:
                message = title_block

            # Préfixe matière en tête suivi d'une parenthèse ouvrante (titre parenthésé) :
            # "Créature Magique (Les Dragons - Cours 1)" → "(Les Dragons - Cours 1)".
            # On retire la matière redondante sans casser le tiret interne de la parenthèse.
            if subject:
                # k >= 2 : une matière est multi-mots ("Créature Magique") ; un mot simple
                # comme "Sort" est du contenu de titre, pas l'étiquette matière — on le garde.
                _mw0 = message.split()
                for _k in (3, 2):
                    if len(_mw0) > _k:
                        _head = ' '.join(_mw0[:_k])
                        _rest = message[len(_head):].lstrip()
                        if _rest.startswith('(') and \
                           _normalize_subject_strict(_head).lower() == subject.lower() and \
                           _trigram_sim(_deaccent(_head), _deaccent(subject)) >= 0.5:
                            message = _rest
                            break

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
                    # Essai sur la partie avant " - ". On utilise la version STRICTE (avec
                    # seuil) : un vrai préfixe matière matche par mot-clé, mais un fragment de
                    # titre ("(Les Nocthraals") ne doit PAS être forcé vers une matière par
                    # simple similarité trigramme — sinon le titre se fait couper à "Cours 2".
                    m_dash2 = re.match(r'^([^–\-]{1,40})\s*[-–]\s*(.+)$', message, re.DOTALL)
                    if m_dash2:
                        norm2 = _normalize_subject_strict(m_dash2.group(1).strip())
                        if norm2 and norm2.lower() == subject.lower():
                            message = m_dash2.group(2).strip()

        # La section icône FiveM (étiquette du jeu) prime pour la matière et la salle :
        # elle est explicite et fiable, contrairement à une devinette depuis le corps.
        if _icon_subject:
            subject = _icon_subject
        if _icon_room:
            room = _icon_room

        # Fallback : année extraite de l'en-tête (avant "ANNONCE DE COURS")
        if not year and _year_from_header:
            year = _year_from_header

        # Normalisation finale : "Ere Année" → "1ère Année". Le suffixe "ère" n'est utilisé QUE
        # par la 1ère année (les autres, 2→7, sont en "ème") → tout chiffre accolé devant "ère"
        # est un garble OCR (le plus souvent le compteur FPS qui bave : "84fps" → "9 ÈRE ANNÉE",
        # "237 ÈRE ANNÉE"…). On retire ce chiffre parasite et on force "1ère".
        if year and re.match(r'^\s*\d*\s*[eèé]re?\s+ann', _deaccent(year), re.IGNORECASE):
            year = re.sub(r'^\s*\d*\s*[eèé]re?', '1ère', year, count=1, flags=re.IGNORECASE)

        # Délai "IMMÉDIATEMENT" (le cours commence tout de suite, pas de "DANS X MINUTES") :
        # c'est un délai valide affiché par le jeu → on le renseigne au lieu de le laisser vide.
        if not delay and re.search(r'\bIMM[EÉ]DIATEMENT\b', text, re.IGNORECASE):
            delay = "Immédiatement"

        # Nettoie les fuites OCR dans le message :
        # Préfixe métadonnées "[année] : [salle] [matière]. [vrai message]" en tête
        # (ex: "E Année : Sai 1 V Dcfm Créature Magique. Encore de la place") → on retire
        # jusqu'au 1er point. Ancré sur un mot de salle (jamais un vrai début de message).
        message = re.sub(
            r'^[:\s]*(?:\d*\s*e?\s*ann[eé]e?\s*:?\s*)?(?:Sai|Sat|Sau|Salle|Cms|Dcfm)\b[^.]*\.\s*(?=[A-ZÀ-Ü])',
            '', message, flags=re.IGNORECASE)
        # Parenthèse ouvrante orpheline en tête suivie d'un tiret (préfixe retiré DANS la
        # parenthèse, ex: "(HDM - Loups-garous … Loup)" → "Loups-garous … Loup")
        message = re.sub(r'^\(\s*[-–]\s*', '', message)
        # Résidu de nom d'auteur en début : token ALL-CAPS avec ponctuation (ex: "STERIJ,VG Potion")
        message = re.sub(r'^[A-ZÀ-Ü][A-ZÀ-Ü0-9\-]*[,\.;][A-ZÀ-Ü0-9,\.;\-]*\S*\s+(?!\()', '', message)
        # Résidu "initiale + Nom propre" en début (ex: "R Greenshadow Club…" → "Club…")
        message = re.sub(r'^[A-ZÀ-Ü]\s+[A-ZÀ-Ü][a-zà-ü]{2,}\s+', '', message)
        # Artefacts OCR : lettres minuscules isolées (émojis mal lus → "g", "s"…)
        # Retire l'abréviation matière "HDM" en tête SAUF si elle est suivie de "(" : dans ce cas
        # c'est le titre voulu par l'annonceur (ex: "HDM (Faction - Vampire)"), pas un écho.
        message = re.sub(r'^(?:hdm|hmd)\s+(?!\()', '', message, flags=re.IGNORECASE)
        message = re.sub(r'^\d*[EÈeè][Mm][Ee]\s+[Aa]nn[eé][ée]?\s*[/\-]?\s*', '', message)  # #176 résidu "Eme Année" en tête
        message = re.sub(r'^[Aa][Nn]\s+(?=[A-ZÀ-Ü])', '', message)  # #166 résidu ".AN X" → "X"
        message = re.sub(r'^[A-ZÀ-Ü][A-ZÀ-Ü0-9\-]*[,\.;][A-ZÀ-Ü0-9,\.;\-]*\S*\s+(?!\()', '', message)  # résidu ALL-CAPS avec ponctuation
        message = re.sub(r'^[A-ZÀ-Ü]\s+[A-ZÀ-Ü][a-zà-ü]{2,}\s+', '', message)  # initiale + Nom propre
        # Fragment de nom d'auteur collé en tête : 1-3 lettres NON-MOT (ex: "IA" de "Heartfilia")
        # suivies de l'article du vrai titre ("Le/La/Les/L'/Un/Une/Des") → on le retire. Le
        # négatif-lookahead protège un vrai article/préposition en tête ("De La Magie" conservé).
        message = re.sub(
            r"^(?!(?:le|la|les|un|une|de|du|des|et|ou|au|aux|en|à|ce|ces|cet|cette|ma|ta|sa|mon|ton|son|nos|vos)\b)"
            r"[A-Za-zÀ-ÿ]{1,3}\s+(?=(?:Le|La|Les|L['’]|Un|Une|Des)\b)",
            '', message, flags=re.IGNORECASE)
        message = re.sub(r'^[a-z]\s+', '', message)          # artefact OCR : minuscule isolée début (émojis mal lus)
        message = re.sub(r'\s+[a-z]\s+', ' ', message)   # au milieu : "Cervorns g X" → "Cervorns X"
        message = re.sub(r'\s+[a-z]$', '', message)          # en fin minuscule
        message = re.sub(r'\s+[A-Z](?:\s+[A-Z]\.?)?$', '', message)          # en fin majuscule isolée ou initiale + majuscule (ex: "Sat F.")
        message = re.sub(r'\s+X(?:\s+|$)', ' ', message)    # artefact OCR : "X" isolée (V mal lu) au milieu ou fin
        # Retire les résidus d'année qui ont fui dans le message (ex: "X Eme Année" / "5ème Année").
        # MAIS conserve "toutes les années" quand c'est de la PROSE (précédé d'une préposition :
        # "Club ouvert à toutes les années") — sinon on casse la phrase : "ouvert à … Venez".
        def _strip_year_residue(m, _orig=message):
            before = _orig[:m.start()].rstrip().lower()
            if re.match(r'(?i)\s*toutes?\b', m.group()) and re.search(r'\b(?:[àa]|aux?|pour|de|des|d[èe]s|sur)$', before):
                return m.group()  # prose : on garde la mention dans la phrase
            return ''
        message = re.sub(_YEAR_RE, _strip_year_residue, message, flags=re.IGNORECASE).strip(' -—,')
        # Retire les suffixes ordinaux orphelins en fin de message (ex: "Cours 2 Eme" → "Cours 2")
        # "2 Eme" vient de "2ème année" dont "année" était dans la section icône et non dans le titre
        message = re.sub(r'\s+[eèêé]m[eé]?\s*$', '', message, flags=re.IGNORECASE).strip(' -—,')
        # Nettoie les doubles virgules laissées par le retrait de l'année (ex: ", , En" → ", En")
        message = re.sub(r',\s*,+', ',', message)
        # Ponctuation orpheline laissée par le retrait de l'année entre deux points
        # (ex: "Sort (Invertum). 3eme années. En …" → "(Invertum). . En …" → "(Invertum). En …")
        message = re.sub(r'\.\s*\.+', '.', message)
        # Format descriptif "Cours de X, Yème années, en salle de Z" : la mention de salle/serre
        # EN FIN de message (précédée d'une ponctuation) est redondante avec le tag salle → on la
        # retire. Ancré sur "[.,] en salle/serre …$" : ne touche pas une phrase ("… attendus en
        # salle de Duel pour …" n'a pas de ponctuation avant "en salle").
        message = re.sub(r'\s*[.,]\s*[Ee]n\s+(?:salle|serre)\b[\w\s\'’-]*$', '', message, flags=re.IGNORECASE)
        # Retire les prépositions isolées en fin de message (ex: "Sort (Luridium), En" → "Sort (Luridium)")
        # Ajouter avant le nettoyage final : retrait de la salle si elle fuit dans le message
        message = re.sub(rf'\s*/\s*{re.escape(room)}.*$', '', message, flags=re.IGNORECASE) if room else message
        # Retrait des artefacts OCR : espaces/chiffres orphelins
        message = re.sub(r'\s+\d\s+\d(?=\s|$)', '', message)
        message = re.sub(r'(?:,\s*)?\b(?:en|de|du|au[x]?|la|le|les|sur|par|pour)\s*$', '', message, flags=re.IGNORECASE)
        # Retire les tokens ALL-CAPS résiduels isolés (fragments de salle/année mal nettoyés)
        message = re.sub(r'\b(?:zrro|rro|ov|cv)\b', '', message, flags=re.IGNORECASE)
        # Résidus de salle OCR en fin de message (ex: "Sat F- Serre" / "Sai 1 Fr CrÃ©atures")
        message = re.sub(r'\s+(?:Sat|Sai|Sau)\s*[F\-\.]+.*$', '', message, flags=re.IGNORECASE)
        # Localisation "Cheminée/Cheminette : <pièce>" en fin de message : indicateur de lieu
        # (destination de cheminette), jamais un morceau du titre du cours → on coupe tout après.
        # Ex: "(Tonique du Vent Abyssal) Cheminée : Salle Des Clubs" → "(Tonique du Vent Abyssal)".
        message = re.sub(r'\s+Chemin\w*\s*:.*$', '', message, flags=re.IGNORECASE)
        # "Cheminée/Cheminette" SEULE en fin (sans ':' ni pièce, ex: "… Partie 1- Cheminée") :
        # étiquette de lieu tronquée → on la retire (jamais un mot de titre en fin).
        message = re.sub(r'\s*[-–]?\s*Chemin(?:[ée]e?|ette)\s*$', '', message, flags=re.IGNORECASE)
        # Parenthèse de LIEU en fin de message — débute par un mot de salle (Salle / Étude /
        # "Salintude" = "Salle Étude" mal lu…) : c'est la salle recopiée dans le titre, déjà
        # extraite dans le champ room → on la retire. Ex: "… (Salintude Golmue)" supprimé.
        # parenthèse fermante OPTIONNELLE : l'OCR coupe souvent la fin ("… (Saliecréature").
        message = re.sub(r'\s*\(\s*(?:sal\w*|[ée]tude)\b[^)]*\)?\s*$', '', message, flags=re.IGNORECASE)
        # ── Métadonnées de LIEU/TEMPS/NOMBRE entre parenthèses recopiées dans le titre ───────
        # Annonceurs collant "(… année)(Salle:X)(N places)" au titre, garblé par l'OCR (parenthèse
        # perdue). On retire ces blocs — complets OU à parenthèse manquante. ⚠️ On se limite aux
        # mots-clés lieu/temps/nombre : PAS les matières/types/potions, qui sont souvent du CONTENU
        # ("Potion (Nanis de Souffle d'Aelyne Pratique)" → conservé). Jamais un mot hors parenthèse
        # ("…toutes les années" conservé) ni une parenthèse sans mot-clé ("HDM (Faction)" conservé).
        _MK = r'(?:ann[ée]es?|salles?|serres?|places?|golm\w*|dcfm|cms)'
        message = re.sub(rf'\s*\([^)]*\b{_MK}\b[^)]*\)', ' ', message, flags=re.IGNORECASE)               # bloc complet
        message = re.sub(rf'\s+(?:\d+\s*[eè]?\s*)?\b{_MK}\b[^()]*\).*$', '', message, flags=re.IGNORECASE)  # parenthèse ouvrante perdue
        message = re.sub(rf'\s*\([^()]*\b{_MK}\b.*$', '', message, flags=re.IGNORECASE)                    # parenthèse fermante perdue
        message = re.sub(r'\s{2,}', ' ', message).strip(' -—,')
        # Résidu de délai tronqué en fin de message : "DANS 3" sans "minute(s)" (l'OCR a coupé
        # le template "Dans X minute(s)"). On retire le "Dans [X]" final → le délai reste vide
        # (capture tronquée) plutôt que de polluer le titre. Ex: "Locus Minor Dans" → "Locus Minor".
        message = re.sub(r'\s+Dans(?:\s+\d{1,3})?\s*$', '', message, flags=re.IGNORECASE)
        # Métadonnée salle recopiée APRÈS le titre, derrière un " - " (ex: "Le Fangor - CA' \\ F :
        # Cheminée: Bibliothèque", "(Les Dragons - Cours 1) - Salle …") : on coupe au " - " dont
        # le segment suivant contient un ":" ou un mot de salle. On ne touche pas un vrai titre à
        # tiret interne ("Boucentête - Et Témoignage" : pas de ":" ni de salle après → conservé).
        message = re.sub(
            r'\s+[-–]\s+[^-–:]*(?::|(?:salle|sai|sat|sau|cms|dcfm|chemin|biblioth|toilett)\w*).*$',
            '', message, flags=re.IGNORECASE)
        # Métadonnée salle en fin sous forme "[Salle/Sai…] [qqch] : [pièce]" (ex: "… Maison)
        # Salle : Littérature", "Sai F : Littérature"). Le ":" la distingue d'une phrase fluide
        # ("… en salle de Duel pour …" n'a pas de ":" après "salle") → on la retire.
        message = re.sub(r'\s+(?:Salle|Serre|Sai|Sat|Sau|Cms|Dcfm)\b[^:]{0,6}:\s*\S.*$', '', message, flags=re.IGNORECASE)
        # Variante : le LABEL "Salle" avant le ":" est garblé par des emojis collés dans l'annonce
        # (ex: jeu "Coquille de Voracité 🧊✨ Salle : Serres" → OCR "… Voracité Ca\" F : Serres").
        # Signature fiable : un ":" suivi d'un MOT DE LIEU (Serre/Salle/Étude/Bibliothèque/…) en
        # fin de message → on coupe, en avalant les 1-3 tokens courts garblés du label. Le mot de
        # lieu après le ":" évite de toucher un vrai titre à deux-points ("Potion : Invertum").
        message = re.sub(
            r'\s+[^\s:]{1,5}(?:\s+[^\s:]{1,5}){0,2}\s*:\s*'
            r'(?:serres?|salles?|[ée]tudes?|biblioth\w*|couloirs?|golm\w*|cms|dcfm)\b.*$',
            '', message, flags=re.IGNORECASE)
        # Format structuré "Titre | Salle X | 2e année | 13h10 | 25 places" (annonces type
        # Livio Lenfield). La présence d'un "|" signale ce bloc de métadonnées : on coupe le
        # message au 1er marqueur = le "|" OU le " Salle/Serre " le plus proche (la vraie salle
        # vient de l'icône). Gardé derrière la présence d'un "|" pour ne pas toucher une phrase
        # normale ("… en salle de Duel …" n'a pas de pipe).
        if '|' in message:
            _cut = re.search(r'\s*\|\s*|\s+(?=(?:Salle|Serre)\b)', message, flags=re.IGNORECASE)
            if _cut and _cut.start() > 5:
                message = message[:_cut.start()].rstrip(' -—,|')
            message = re.sub(r'\s+\d{1,2}$', '', message)  # séparateur "1" orphelin laissé en fin
        # Métadonnées du popup recopiées dans le message après un séparateur "/" :
        # "… / Salle … / 18H20 | 25 Places" → on coupe à partir du 1er bloc méta
        # (salle, horaire HHhMM/HHHMM, "N places").
        message = re.sub(r'\s*[/|]\s*(?:sa[lit]\w*|sai\b|sat\b)\b.*$', '', message, flags=re.IGNORECASE)
        message = re.sub(r'\s*[/|]?\s*\d{1,2}\s*[Hh]\s*\d{2}\b.*$', '', message)
        message = re.sub(r'\s*[/|]\s*\d+\s*places?\b.*$', '', message, flags=re.IGNORECASE)
        # "24 Places" sans séparateur (compteur de places du popup) → retirer
        message = re.sub(r'\s+\d{1,3}\s*places?\b', '', message, flags=re.IGNORECASE)
        # Résidu d'année sans chiffre en fin ("… Eme Année", "… Ere Année") → retirer
        message = re.sub(r'\s+[eèé]m?e?\s+ann[eé]e?\s*$', '', message, flags=re.IGNORECASE).strip(' -—,')
        # Résidus d'overlay FiveM (stats numériques résiduels après nettoyage FPS/GPU)
        message = re.sub(r'\b\d{2,3}[/%]\d*\b', '', message)
        # Écho de la matière recopié en fin de message (ex: "… Couloir Histoires De La Magie",
        # "… Méchant Loup) Histoires De La Magie 111 11") : on le retire si la fin du message,
        # normalisée, redonne exactement la matière déjà extraite (vrai écho, pas un mot du titre).
        if subject:
            _mw = message.split()
            # Chiffres orphelins en fin : on ne les retire que s'ils ressemblent à du BRUIT OCR
            # (≥2 tokens chiffrés consécutifs, ou un seul ≥3 chiffres). Un petit numéro de leçon
            # légitime ("Cours 2", "Partie 3") est CONSERVÉ — il fait partie du titre.
            _nd = 0
            while _nd < len(_mw) and re.fullmatch(r'\d{1,3}', _mw[-1 - _nd]):
                _nd += 1
            if _nd >= 2 or (_nd == 1 and len(_mw[-1]) >= 3):
                _mw = _mw[:-_nd]
            for _n in (1, 2, 3, 4, 5):  # ordre croissant = écho minimal (ne mange pas un vrai mot du titre)
                # Un écho d'1 mot (la matière seule, ex: "Luridium Sorts" → "Luridium") peut être
                # retiré même s'il ne reste qu'1 mot ; pour ≥2 mots d'écho on garde la marge de
                # sécurité (≥2 mots restants) car le risque de manger un vrai mot augmente.
                _min_left = _n if _n == 1 else _n + 1
                if len(_mw) > _min_left:
                    _tail = ' '.join(_mw[-_n:])
                    # Vrai écho si la fin, normalisée, redonne la matière ET reste proche
                    # (trigrammes ≥0.6 OU distance d'édition ≤0.4 — la 2e attrape les échos très
                    # garblés "Créature Magicuje"≈"Créatures Magiques", ratio 0.22 ; sans laisser
                    # passer un vrai mot de titre "Sortilège…"≈"Sorts", ratio 0.76).
                    if _normalize_subject_strict(_tail).lower() == subject.lower() and (
                           _trigram_sim(_deaccent(_tail), _deaccent(subject)) >= 0.6 or
                           _lev_ratio(_deaccent(_tail).lower(), _deaccent(subject).lower()) <= 0.4):
                        _mw = _mw[:-_n]
                        break
            message = ' '.join(_mw).strip(' -—,')
        message = re.sub(r'\s*[/|]\s*$', '', message)      # séparateur popup orphelin en fin ("Le Spectrevif /")
        # Parenthèse fermante orpheline en fin (le "(" correspondant a été retiré) → on l'enlève
        if message.count('(') < message.count(')'):
            message = re.sub(r'\s*\)\s*$', '', message)
        message = re.sub(r'\s*-\s*-+\s*', ' - ', message)  # double tiret OCR ("- -") → " - "
        # Template "EN [salle] SUR [titre]" (annonces type Leon Lonweack / Lydia Clarke) :
        # quand le message DÉBUTE par une localisation ("En Sai…/En Salle…/En Cms…") suivie
        # d'un "Sur …", le vrai contenu du cours est la partie "Sur …" — on retire le préfixe
        # localisation. Ancré en tête : ne touche pas une vraie phrase ("Les élèves … sur le …").
        _m_sur = re.match(
            r'^En\s+(?:Sai|Sat|Sau|Salle|Serre|Cms|Dcfm|Duel)\b.*?\s+(Sur\s+.+)$',
            message, re.IGNORECASE | re.DOTALL
        )
        if _m_sur:
            message = _m_sur.group(1).strip()
        # Préfixe "Sur " résiduel du template "COURS … SUR [titre]" : le vrai titre est la suite
        # (ex: "Sur Le Grand Méchant Loup" → "Le Grand Méchant Loup"). "Sur" collé ("Surnaturel")
        # n'est pas touché (espace requis).
        message = re.sub(r'^Sur\s+', '', message, flags=re.IGNORECASE)
        # Résidu "- '" / "- :" en fin : tiret suivi seulement de ponctuation (vient d'un
        # "- Salle:" dont la salle a été retirée, le "Salle:" lu "'"). Ex: "(Le Tanuki) - '".
        message = re.sub(r"\s*[-–]\s*['’\"().,:;]*\s*$", '', message)
        message = re.sub(r'\s*\(\s*\)\s*', ' ', message)   # parenthèses vides (contenu retiré, ex: "(HDM)" → "()")
        message = re.sub(r'\s{2,}', ' ', message).strip(' ,;-—')

        # Fallback : le corps n'a pas produit de titre exploitable.
        # 1) Si le titre avait été happé dans l'icône (ex: "Club - Musique"), on l'utilise,
        #    en retirant le préfixe matière redondant ("Club - Musique" → "Musique").
        # 2) Sinon, à défaut, on affiche la catégorie (ex: "Sorts" en Salle Généraliste)
        #    plutôt que de jeter l'annonce.
        if len(message) < 4 and _icon_title_raw:
            _t = _smart_title(_icon_title_raw)
            _t = re.sub(r'^[^-–]{1,30}[-–]\s*', '', _t).strip()  # retire "Matière - "
            if len(_t) >= 3:
                message = _t
        if len(message) < 4 and subject:
            message = subject

        # Rejette faux positifs OCR
        if len(author) < 3 or len(message) < 4:
            return None

        # "11"/"111" = chiffres romains "II"/"III" mal lus (les "I" collés ressemblent à des "1").
        # Les années ne dépassant pas 7, une suite de "1" en début d'année est en réalité un
        # romain → on convertit (11→2ème, 111→3ème) AVANT le rejet des années > 7.
        if year:
            _m1 = re.match(r'^\s*(1{2,3})\s*[eèé]?m', _deaccent(year), re.IGNORECASE)
            if _m1:
                year = f"{len(_m1.group(1))}ème année"
        # Année impossible (8ème+ : il n'existe que 1ère→7ème + "Toutes années") = garble OCR
        # → champ vide plutôt qu'une valeur fausse (ex: "11 EME ANNÉE"). Conforme zéro-faute.
        if year and re.search(r'\b(?:[89]|[1-9]\d)\s*[èeé]?m', year, re.IGNORECASE):
            year = ""
        message = _strip_emoji_garble(message)  # retire un emoji mal lu en fin (ex: "üò-ee")
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

        # Title-case (l'OCR rend l'annonce générique tout en MAJUSCULES) — cohérent avec
        # le rendu des annonces de cours. "ADEL SINA" → "Adel Sina", "VERVENINI LES
        # CANDIDATS …" → "Vervenini Les Candidats …".
        author = _smart_title(author)
        message = _smart_title(message)
        message = _strip_emoji_garble(message)  # retire un emoji mal lu en fin (ex: "üò-ee")

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
        payload: dict = {"exeToken": tok, "announcement": ann, "version": VERSION}
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
        zip_path    = install_dir / "CourSW_update.zip"
        new_dir     = install_dir / "CourSW_new"
        bat_path    = install_dir / "update.bat"

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
            f'cd /d "{install_dir}"\n'
            'timeout /t 4 /nobreak > nul\n'
            f'robocopy "{extracted}" "{install_dir}" /E /IS /IT /IM /NFL /NDL /NJH /NJS\n'
            f'rmdir /s /q "{new_dir}" 2>nul\n'
            f'del "{zip_path}" 2>nul\n'
            f'start "" "{exe_path}" --updated\n'
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
        # Si on vient d'une mise à jour auto, attendre 30 min avant la prochaine vérif
        initial_delay = 1800 if '--updated' in sys.argv else 600
        time.sleep(initial_delay)
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

        # WS_EX_NOACTIVATE : empêche la fenêtre de voler le focus/la souris à FiveM
        self.after(100, self._set_noactivate)

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

        # Vérification GitHub au démarrage — sauf si on vient d'une mise à jour auto
        # (évite la boucle infinie quand le bot OCR pousse des versions en rafale)
        if '--updated' not in sys.argv:
            threading.Thread(
                target=check_github_update,
                args=(lambda m: self.after(0, self._log, m), self._notify),
                daemon=True
            ).start()
        else:
            self.after(0, self._log, f"✅ Mise à jour appliquée — v{VERSION} en cours d'exécution")

        tok = load_token()
        if tok:
            self._start(tok)
        else:
            self._show_window()
            self._ask_link()

    def _set_noactivate(self):
        """Applique WS_EX_NOACTIVATE pour ne jamais voler le focus à FiveM."""
        try:
            hwnd = self.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x08000000)  # WS_EX_NOACTIVATE
        except Exception:
            pass

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
        tk.Button(bf, text="🔄  Mises à jour",
                  command=self._check_update_manual,
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

    def _check_update_manual(self):
        self._log("🔄 Recherche manuelle de mise à jour…")
        threading.Thread(
            target=check_github_update,
            args=(lambda m: self.after(0, self._log, m), self._notify),
            daemon=True
        ).start()

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("📡 CourSW — Seven Wands", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ouvrir", self._show_window, default=True),
            pystray.MenuItem("Ouvrir le site", lambda: webbrowser.open(SITE_URL)),
            pystray.MenuItem("Changer de compte", lambda: self.after(0, self._ask_link)),
            pystray.MenuItem("Chercher les mises à jour", lambda: self.after(0, self._check_update_manual)),
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


# Handle du mutex d'instance unique — gardé en global pour que le handle reste ouvert
# pendant toute la vie du process (Windows libère le mutex automatiquement à la fermeture
# ou au crash, donc aucun verrou résiduel à nettoyer).
_SINGLE_INSTANCE_MUTEX = None


def _ensure_single_instance():
    """Empêche le lancement d'une 2e instance du .exe. Utilise un mutex nommé Windows :
    si une instance tourne déjà, on prévient l'utilisateur et on quitte immédiatement."""
    global _SINGLE_INSTANCE_MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        # Nom unique de l'application (session courante). CreateMutexW réussit toujours mais
        # GetLastError == ERROR_ALREADY_EXISTS (183) si le mutex existait déjà = instance active.
        _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "CoursSW_SingleInstance_Mutex")
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            try:
                import tkinter as tk
                from tkinter import messagebox
                _r = tk.Tk()
                _r.withdraw()
                _r.attributes("-topmost", True)
                messagebox.showinfo(
                    "CoursSW est déjà ouvert",
                    "L'application CoursSW est déjà en cours d'exécution.\n\n"
                    "Regarde dans la barre des tâches ou la zone de notification "
                    "(près de l'horloge, en bas à droite).",
                    parent=_r,
                )
                _r.destroy()
            except Exception:
                pass
            sys.exit(0)
    except Exception:
        # En cas d'échec inattendu de l'API, on ne bloque pas le lancement.
        pass


def main():
    _ensure_single_instance()  # une seule instance à la fois
    app = App()
    app.protocol("WM_DELETE_WINDOW", app._hide_window)  # croix = réduire dans le tray
    app.mainloop()


if __name__ == "__main__":
    main()
