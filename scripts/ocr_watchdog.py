"""OCR Watchdog — analyse les annonces et corrige main.py automatiquement."""
import anthropic
import requests
import json
import os
import re
import base64
import sys
import datetime

SITE = "https://almanach-peh.vercel.app"
ANALYZED_FILE = "CoursSW/.ocr_analyzed_ids.txt"
MAIN_PY = "CoursSW/main.py"

# --- IDs déjà analysés ---
analyzed = set()
if os.path.exists(ANALYZED_FILE):
    with open(ANALYZED_FILE) as f:
        analyzed = set(l.strip() for l in f if l.strip())

# --- Annonces récentes ---
try:
    r = requests.get(f"{SITE}/api/cours/announce", timeout=15)
    raw = r.json()
    # L'API peut retourner une liste de dicts ou de strings JSON
    announcements = []
    items = raw if isinstance(raw, list) else raw.get("announcements", raw.get("data", []))
    for item in items:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except Exception:
                continue
        if isinstance(item, dict):
            announcements.append(item)
except Exception as e:
    print(f"Erreur fetch announce: {e}")
    sys.exit(0)

print(f"Total annonces: {len(announcements)}")

nouvelles = [
    a for a in announcements
    if a.get("type") == "cours"
    and a.get("id") not in analyzed
    and (a.get("hasOcrLog") or a.get("hasScreenshot"))
]

if not nouvelles:
    print("Aucune nouvelle annonce.")
    sys.exit(0)

print(f"{len(nouvelles)} nouvelle(s) annonce(s).")

# --- Lire main.py ---
with open(MAIN_PY, encoding="utf-8") as f:
    main_py_content = f.read()

# Extraire la section parsing uniquement (réduit les tokens de 80%)
relevant_keywords = (
    "_NAME_", "_STOP", "_ROOMS", "_CAPS", "_ALL_", "VERSION",
    "def _parse", "def _extract", "def _clean", "def _best",
    "_PREPOSITIONS", "_YEAR", "_DELAY", "_SUBJECT"
)
lines = main_py_content.split("\n")
parsing_lines = []
i = 0
while i < len(lines):
    if any(kw in lines[i] for kw in relevant_keywords):
        start = max(0, i - 1)
        end = i + 1
        while end < len(lines) and (
            lines[end].startswith(" ") or lines[end].startswith("\t") or lines[end].strip() == ""
        ):
            end += 1
        parsing_lines.extend(lines[start:end])
        parsing_lines.append("")
        i = end
    else:
        i += 1
parsing_section = "\n".join(parsing_lines) if parsing_lines else main_py_content

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
fixes_applied = []
anomalies_run = []

for ann in nouvelles:
    aid = ann["id"]
    print(f"\nAnalyse {aid}...")

    ocr_log = None
    if ann.get("hasOcrLog"):
        try:
            r = requests.get(f"{SITE}/api/cours/ocrlog/{aid}", timeout=10)
            ocr_log = r.json().get("log")
        except Exception:
            pass

    screenshot_b64 = None
    if ann.get("hasScreenshot"):
        try:
            r = requests.get(f"{SITE}/api/cours/screenshot/{aid}", timeout=15)
            if r.status_code == 200:
                screenshot_b64 = base64.b64encode(r.content).decode()
        except Exception:
            pass

    author = ann.get("author", "(vide)")
    title = ann.get("title", "(vide)")
    room = ann.get("room", "(vide)")
    year = ann.get("year", "(vide)")
    message = ann.get("message", "(vide)")
    delay = ann.get("delay", "(vide)")

    prompt = f"""Tu analyses une annonce de cours parsée par OCR. Détecte si le parsing est incorrect.

Champs parsés :
- author: {author}
- title: {title}
- room: {room}
- year: {year}
- message: {message}
- delay: {delay}
"""
    if ocr_log:
        prompt += f"\nTexte brut OCR :\n```\n{ocr_log}\n```\n"

    prompt += f"""
Section parsing de main.py :
```python
{parsing_section}
```

Anomalies à détecter :
1. author contient des tokens non-nominatifs tout en majuscules (C'EST, DE, EN, DU, LE...)
2. author a plus de 3 tokens
3. message commence par une préposition (de, du, d', des, en, la, le, les, au, aux)
4. message contient ",," ou ", ,"
5. message se termine par une préposition isolée
6. message commence par pattern "Initiale Nom" (ex: "R Greenshadow", "F. Dupont")
7. room clairement incorrect (ne correspond pas à une salle de Poudlard visible dans l'OCR/screenshot)
8. title avec préposition dupliquée ("Cours de de X")

RÈGLE ABSOLUE : corriger UNIQUEMENT si l'anomalie est clairement certaine.

Si anomalie détectée, réponds en JSON :
{{"anomalie": "description courte", "fix_old": "texte exact dans main.py", "fix_new": "remplacement"}}

Si aucune anomalie : réponds uniquement {{"anomalie": null}}
"""

    content = []
    if screenshot_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}
        })
    content.append({"type": "text", "text": prompt})

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": content}]
        )
        result_text = resp.content[0].text.strip()
        print(f"Réponse: {result_text[:300]}")

        # Extraire le premier JSON valide (Claude peut ajouter du texte après)
        result = None
        for m in re.finditer(r'\{', result_text):
            try:
                end = result_text.rindex('}', m.start()) + 1
                result = json.loads(result_text[m.start():end])
                break
            except Exception:
                continue

        if result is None:
            print("Pas de JSON valide dans la réponse.")
            anomalies_run.append({"id": aid, "description": "(réponse invalide)", "fixed": False})
        elif result.get("anomalie"):
            anomalie = result["anomalie"]
            print(f"Anomalie : {anomalie}")
            anomalies_run.append({"id": aid, "description": anomalie[:120], "fixed": False})
            # Créer une GitHub Issue pour que Claude (VS Code) corrige proprement
            import subprocess
            screenshot_url = f"{SITE}/api/cours/screenshot/{aid}" if ann.get("hasScreenshot") else "(pas de screenshot)"
            issue_body = f"""## Anomalie OCR détectée automatiquement

**Annonce ID :** `{aid}`

**Champs parsés :**
- author: `{author}`
- title: `{title}`
- room: `{room}`
- year: `{year}`
- message: `{message}`

**Anomalie détectée :** {anomalie}

**Screenshot :** {screenshot_url}

**Log OCR brut :**
```
{ocr_log or "(non disponible)"}
```

---
*Créé automatiquement par OCR Watchdog — à corriger dans `main.py`*
"""
            result_gh = subprocess.run(
                ["gh", "issue", "create",
                 "--repo", "paolito13/CoursSW",
                 "--title", f"[OCR] {anomalie[:80]}",
                 "--body", issue_body,
                 "--label", "ocr-anomaly"],
                capture_output=True, text=True
            )
            if result_gh.returncode == 0:
                print(f"Issue créée : {result_gh.stdout.strip()}")
            else:
                print(f"Erreur création issue : {result_gh.stderr[:200]}")
        else:
            print("Aucune anomalie.")
    except Exception as e:
        print(f"Erreur Claude: {e}")

    analyzed.add(aid)

# --- Sauvegarder IDs ---
with open(ANALYZED_FILE, "w") as f:
    f.write("\n".join(sorted(analyzed)) + "\n")

# --- Lire la version de main.py ---
version = "?"
try:
    with open(MAIN_PY, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^VERSION\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                version = m.group(1)
                break
except Exception:
    pass

# --- Envoyer le résumé au site (Redis via API) ---
run_summary = {
    "ts": int(datetime.datetime.utcnow().timestamp() * 1000),
    "version": version,
    "analyzed": len(nouvelles),
    "anomalies": anomalies_run,
}
try:
    r = requests.post(
        f"{SITE}/api/cours/watchdog-history",
        json=run_summary,
        timeout=10
    )
    if r.status_code == 200:
        print("Résumé envoyé au site.")
    else:
        print(f"Erreur envoi résumé: {r.status_code}")
except Exception as e:
    print(f"Erreur envoi résumé: {e}")

print("\nAnalyse terminée.")
sys.exit(0)
