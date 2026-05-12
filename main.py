"""
CourSW.exe — Observateur d'annonces FiveM pour Seven Wands
Détecte automatiquement FiveM, capture les annonces, les envoie au site.
Compatible Windows 10/11. Aucune installation manuelle requise.
"""

import sys
import os
import subprocess
import importlib

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
VERSION        = "1.0.0"
SITE_URL       = "https://almanach-peh.vercel.app"
API_LINK       = f"{SITE_URL}/api/cours/link"
API_HEARTBEAT  = f"{SITE_URL}/api/cours/heartbeat"
API_ANNOUNCE   = f"{SITE_URL}/api/cours/announce"

TOKEN_FILE         = Path(os.environ.get("APPDATA", ".")) / "CourSW" / "token.json"
CAPTURE_INTERVAL   = 1.0
HEARTBEAT_INTERVAL = 30

# Zone capture : 27% gauche × 38% haut (s'adapte à toute résolution)
CAP_RIGHT  = 0.27
CAP_BOTTOM = 0.38


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

def ocr_image(pil_img: Image.Image) -> str:
    if _USE_WIN_OCR:
        try:
            loop = asyncio.new_event_loop()
            text = loop.run_until_complete(_win_ocr_async(pil_img))
            loop.close()
            return text
        except Exception:
            pass

    if _USE_TESSERACT:
        try:
            return pytesseract.image_to_string(pil_img, lang="fra+eng")
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
        return np.array(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))

# ── Parsing OCR → annonce structurée ─────────────────────────────────────────
def parse_announcement(text: str) -> dict | None:
    if not text.strip():
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    joined = " ".join(lines)

    is_cours   = bool(re.search(r'ANNONCE\s+DE\s+COURS', joined, re.IGNORECASE))
    is_general = bool(re.search(r'ANNONCE\s+DE\s+\w', joined, re.IGNORECASE)) and not is_cours

    if not is_cours and not is_general:
        return None

    author = ""
    if is_cours:
        m = re.search(r'[Pp]ar\s+([A-ZÀ-Ü][a-zA-ZÀ-ü\s\-]+?)(?:\s{2,}|\n|$)', joined)
        if m: author = m.group(1).strip()
    else:
        m = re.search(r'ANNONCE\s+DE\s+([A-ZÀ-Ü][a-zA-ZÀ-ü\s\-]+?)(?:\s{2,}|\n|$)', joined, re.IGNORECASE)
        if m: author = m.group(1).strip()

    # Filtre les lignes d'en-tête pour récupérer le message
    skip_re = re.compile(r'annonce de cours|annonce de|^par\s', re.IGNORECASE)
    body = [l for l in lines if not skip_re.search(l) and author.lower() not in l.lower()]

    delay   = next((l for l in body if re.search(r'dans\s+\d+\s+min', l, re.IGNORECASE)), None)
    year    = next((l for l in body if re.search(r'\d\s*[eè]me\s+ann', l, re.IGNORECASE)), None)
    body    = [l for l in body if l not in (delay or "", year or "")]
    message = " ".join(body).strip()

    if not message and not author:
        return None

    ann: dict = {"type": "cours" if is_cours else "general", "author": author, "message": message}
    if delay: ann["delay"] = delay
    if year:  ann["year"]  = year
    return ann

def ann_hash(ann: dict) -> str:
    return hashlib.md5(f"{ann['type']}:{ann.get('author','')}:{ann.get('message','')}".encode()).hexdigest()

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
        r = requests.post(API_HEARTBEAT, json={"exeToken": tok, "version": VERSION}, timeout=5)
        return r.json() if r.ok or r.status_code == 200 else {}
    except Exception: return {}

def send_announcement(tok: str, ann: dict) -> bool:
    try:
        return requests.post(API_ANNOUNCE, json={"exeToken": tok, "announcement": ann}, timeout=5).ok
    except Exception: return False

def link_token(one_time: str) -> str | None:
    try:
        r = requests.post(API_LINK, json={"token": one_time}, timeout=10)
        if r.ok: return r.json().get("exeToken")
    except Exception: pass
    return None

# ── Worker ────────────────────────────────────────────────────────────────────
def _do_self_update(download_url: str, on_log):
    """Télécharge la nouvelle version, remplace l'exe via un script batch, redémarre."""
    try:
        exe_path = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve()
        new_path = exe_path.with_suffix('.new.exe')
        bat_path = exe_path.with_suffix('.update.bat')

        on_log("⬇️  Téléchargement de la mise à jour…")
        with requests.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(new_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)

        # Script batch qui attend la fermeture de l'exe, remplace, puis relance
        bat = f"""@echo off
ping 127.0.0.1 -n 3 > nul
move /y "{new_path}" "{exe_path}"
start "" "{exe_path}"
del "%~f0"
"""
        bat_path.write_text(bat, encoding='utf-8')
        on_log("✅ Mise à jour téléchargée — redémarrage…")
        time.sleep(1)
        subprocess.Popen(["cmd", "/c", str(bat_path)], creationflags=subprocess.DETACHED_PROCESS)
        os._exit(0)
    except Exception as e:
        on_log(f"⚠️  Mise à jour échouée : {e}")


class Worker(threading.Thread):
    def __init__(self, exe_token: str, on_status, on_log):
        super().__init__(daemon=True)
        self.tok = exe_token
        self.on_status = on_status
        self.on_log = on_log
        self.running = True
        self.seen: dict[str, float] = {}
        self.last_hb = 0.0

    def stop(self): self.running = False

    def run(self):
        self.on_log("Démarrage de la surveillance…")
        # Heartbeat immédiat au démarrage
        try:
            hb = send_heartbeat(self.tok)
            self.on_log(f"Heartbeat initial : {hb}")
        except Exception as e:
            self.on_log(f"❌ Erreur heartbeat initial : {e}")
            hb = {}
        self.last_hb = time.time()
        if hb.get("update_required"):
            self.on_status("🔄 Mise à jour requise…")
            self.on_log("⚠️  Nouvelle version requise — mise à jour automatique…")
            dl = hb.get("download_url", "")
            if dl:
                threading.Thread(target=_do_self_update, args=(dl, self.on_log), daemon=True).start()
            return
        if not hb.get("ok"):
            self.on_log(f"⚠️  Heartbeat refusé — token invalide ou site inaccessible")
        self.on_status("🟢 Connecté — surveillance active" if hb.get("ok") else "🔴 Impossible de joindre le site")
        # Ouvre le site directement sur l'onglet Cours
        webbrowser.open(f"{SITE_URL}?section=cours")

        self.on_log(f"Entrée boucle — running={self.running}")
        while self.running:
          try:
            now = time.time()
            self.on_log(f"Tick boucle")

            # Heartbeat
            if now - self.last_hb > HEARTBEAT_INTERVAL:
                hb = send_heartbeat(self.tok)
                self.last_hb = now

                if hb.get("update_required"):
                    self.on_status("🔄 Mise à jour requise…")
                    self.on_log("⚠️  Nouvelle version requise — mise à jour automatique…")
                    dl = hb.get("download_url", "")
                    if dl:
                        threading.Thread(target=_do_self_update, args=(dl, self.on_log), daemon=True).start()
                    return

                self.on_status("🟢 Connecté — surveillance active" if hb.get("ok") else "🔴 Impossible de joindre le site")

            # FiveM
            win = find_fivem_window()
            if not win:
                self.on_log("FiveM non détecté, nouvelle tentative dans 5s…")
                time.sleep(5)
                continue
            self.on_log(f"FiveM détecté : rect={win[1]}")

            _, rect = win
            ww = rect[2] - rect[0]
            wh = rect[3] - rect[1]

            try:
                frame = capture_region(rect)
                pil   = Image.fromarray(frame)
                text  = ocr_image(pil)
                ann   = parse_announcement(text) if text.strip() else None

                if ann:
                    h = ann_hash(ann)
                    if now - self.seen.get(h, 0) > 300:
                        self.seen[h] = now
                        ok = send_announcement(self.tok, ann)
                        label = "cours" if ann["type"] == "cours" else "générique"
                        self.on_log(
                            f"{'✅' if ok else '⚠️'} Annonce {label} "
                            f"({ann.get('author','?')}) : {ann.get('message','')[:45]}…"
                        )

                # Nettoyage hashes > 10 min
                self.seen = {k: v for k, v in self.seen.items() if now - v < 600}

            except Exception as e:
                self.on_log(f"Erreur : {e}")

            time.sleep(CAPTURE_INTERVAL)

          except Exception as e:
            self.on_log(f"❌ Erreur boucle : {type(e).__name__}: {e}")
            time.sleep(2)

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
        )
        self.worker.start()

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
        super().destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app._hide_window)  # croix = réduire dans le tray
    app.mainloop()


if __name__ == "__main__":
    main()
