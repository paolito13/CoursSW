"""OCR Watchdog — analyse les annonces et crée des issues GitHub pour correction."""
import anthropic
import requests
import json
import os
import re
import base64
import sys
import datetime
import subprocess

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

# Extraire la fonction parse_announcement complète
m_parse = re.search(r'def parse_announcement\(.*?(?=\ndef |\Z)', main_py_content, re.DOTALL)
parsing_section = m_parse.group(0)[:6000] if m_parse else main_py_content[:6000]

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
anomalies_run = []


# ═══════════════════════════════════════════════════════════════════════════════
# COUCHE 1 : Détection hardcodée (rapide, sans IA)
# ═══════════════════════════════════════════════════════════════════════════════
PREPOSITIONS = r'^(?:de|du|d\'|des|en|la|le|les|au|aux|un|une|sur|par|avec|dans|pour|vers)\s'
KNOWN_ROOMS = [
    "salle", "etude", "golmu", "potions", "botanique", "serres", "astronomie",
    "transfiguration", "defense", "magie", "histoire", "arithmancie", "divination",
    "soins", "creatures", "medecine", "infirmerie", "biblioth", "cheminee", "toilettes",
    "generaliste", "arts", "musique", "litterature", "runic", "occlumence",
]

def hardcoded_anomalies(ann: dict) -> list[str]:
    """Retourne une liste d'anomalies détectées sans IA."""
    issues = []
    author = ann.get("author", "")
    message = ann.get("message", "")
    room = ann.get("room", "")
    year = ann.get("year", "")

    # Author
    if author:
        tokens = author.split()
        # Chiffre ou ponctuation parasite dans l'auteur
        if re.search(r'\d', author):
            issues.append(f"author contient un chiffre : '{author}'")
        # Token tout-majuscules (abréviation OCR) dans l'auteur
        all_caps = [t for t in tokens if len(t) >= 2 and t.isupper() and t.isalpha()]
        if all_caps:
            issues.append(f"author contient token(s) tout-majuscules : {all_caps}")
        # Trop de tokens (> 3 mots)
        if len(tokens) > 3:
            issues.append(f"author trop long ({len(tokens)} tokens) : '{author}'")
        # Un seul token très court (prénom tronqué ?)
        if len(tokens) == 1 and len(author) <= 3:
            issues.append(f"author suspicieusement court : '{author}'")
        # Commence par une préposition / déterminant
        if re.match(PREPOSITIONS, author, re.IGNORECASE):
            issues.append(f"author commence par une préposition : '{author}'")

    # Message
    if message:
        # Commence par une préposition
        if re.match(PREPOSITIONS, message, re.IGNORECASE):
            issues.append(f"message commence par une préposition : '{message[:60]}'")
        # Virgule doublée (artefact de nettoyage)
        if re.search(r',\s*,', message):
            issues.append(f"message contient double virgule : '{message[:60]}'")
        # Se termine par une préposition isolée
        if re.search(r'\s(?:de|du|d\'|des|en|la|le|les|au|aux|sur|par|avec|dans|pour|vers)\s*$', message, re.IGNORECASE):
            issues.append(f"message se termine par une préposition : '{message[-40:]}'")
        # Commence par "Initiale Nom" (résidu auteur)
        if re.match(r'^[A-ZÀ-Ü]\s+[A-ZÀ-Ü][a-zà-ü]{2,}', message):
            issues.append(f"message commence par pattern 'Initiale Nom' (résidu auteur) : '{message[:40]}'")
        # Chiffres orphelins (résidus overlays FiveM)
        if re.search(r'\b\d{2,4}\b', message) and not re.search(r'\b\d+\s*(?:e|è|ème|iere?|ère?)\b', message, re.IGNORECASE):
            issues.append(f"message contient chiffres orphelins (résidu overlay ?) : '{message[:60]}'")
        # Lettre minuscule isolée parasite au milieu du message
        if re.search(r'(?<=[a-zà-üA-ZÀ-Ü])\s+[a-z]\s+(?=[A-ZÀ-Ü])', message):
            issues.append(f"message contient lettre isolée parasite : '{message[:80]}'")
        # Majuscule ALL-CAPS résiduelle en milieu de message
        if re.search(r'\b[A-ZÀ-Ü]{4,}\b', message):
            m_caps = re.findall(r'\b[A-ZÀ-Ü]{4,}\b', message)
            issues.append(f"message contient token(s) ALL-CAPS résiduel(s) : {m_caps}")
        # Message trop court (< 4 chars) ou vide
        if len(message.strip()) < 4:
            issues.append(f"message trop court : '{message}'")
        # Message commence par "Eme Année" (résidu année mal nettoyé)
        if re.match(r'^[eèêé]m[eé]\s+ann[eé][ée]?', message, re.IGNORECASE):
            issues.append(f"message commence par résidu d'année : '{message[:40]}'")

    # Room
    if room:
        room_lower = room.lower()
        # Aucun mot-clé salle connu → salle possiblement incorrecte
        if not any(kw in room_lower for kw in KNOWN_ROOMS):
            issues.append(f"room ne contient pas de mot-clé connu : '{room}'")
        # Room contient des chiffres (OCR artefact)
        if re.search(r'\d', room):
            issues.append(f"room contient des chiffres : '{room}'")

    # Year
    if year:
        # Format invalide
        if not re.search(r'\d|[IVX]+|toutes?', year, re.IGNORECASE):
            issues.append(f"year format inhabituel : '{year}'")

    return issues


def create_issue(aid: str, ann: dict, anomalie: str, ocr_log: str | None) -> None:
    author = ann.get("author", "(vide)")
    room = ann.get("room", "(vide)")
    year = ann.get("year", "(vide)")
    message = ann.get("message", "(vide)")
    delay = ann.get("delay", "(vide)")
    screenshot_url = f"{SITE}/api/cours/screenshot/{aid}" if ann.get("hasScreenshot") else "(pas de screenshot)"

    issue_body = f"""## Anomalie OCR détectée automatiquement

**Annonce ID :** `{aid}`

**Champs parsés :**
- author: `{author}`
- room: `{room}`
- year: `{year}`
- message: `{message}`
- delay: `{delay}`

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
        print(f"  ✅ Issue créée : {result_gh.stdout.strip()}")
    else:
        print(f"  ❌ Erreur issue : {result_gh.stderr[:200]}")


# ═══════════════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════
for ann in nouvelles:
    aid = ann["id"]
    print(f"\n{'─'*60}\nAnalyse {aid}...")

    # Récupérer OCR log et screenshot
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

    # ── COUCHE 1 : détection hardcodée ────────────────────────────────────────
    hc_issues = hardcoded_anomalies(ann)
    if hc_issues:
        anomalie_hc = " | ".join(hc_issues)
        print(f"  🔴 Hardcoded: {anomalie_hc}")
        anomalies_run.append({"id": aid, "description": anomalie_hc[:120], "fixed": False})
        create_issue(aid, ann, anomalie_hc, ocr_log)
        analyzed.add(aid)
        continue  # Pas besoin de passer par l'IA

    # ── COUCHE 2 : analyse IA (Sonnet) pour cas subtils ───────────────────────
    author = ann.get("author", "(vide)")
    message = ann.get("message", "(vide)")
    room = ann.get("room", "(vide)")
    year = ann.get("year", "(vide)")
    delay = ann.get("delay", "(vide)")

    prompt = f"""Tu es un vérificateur OCR pour des annonces de cours d'une école de magie FiveM (Seven Wands / Poudlard).
Compare le texte OCR brut avec les champs parsés et identifie toute erreur de parsing.

**Champs parsés :**
- author: {author}
- room: {room}
- year: {year}
- message: {message}
- delay: {delay}

**Texte OCR brut :**
```
{ocr_log or "(non disponible — utilise le screenshot)"}
```

**Règles du parsing :**
- Le texte commence toujours par "ANNONCE DE COURS PAR [NOM PROFESSEUR]"
- Suit ensuite le titre du cours, l'année, la salle, et le délai avant le cours
- Les salles connues : Salle Potions, Salle Botanique/Serres, Salle Étude de Golmu, Salle Créatures Magiques, Salle Astronomie, Salle Transfiguration, Salle Défense, Salle Histoire de la Magie, etc.
- Les années : 1ère à 7ème Année (parfois "Toutes années")
- Les noms de profs sont en Title Case (Prénom Nom)

**Anomalies à chercher (sois exhaustif, ne pas hésiter à signaler) :**
1. Author incorrect : mauvais nom, tronqué, contient des tokens parasites (chiffres, abréviations OCR, stop words)
2. Message incorrect : contient des artefacts OCR, est tronqué, contient des fragments de la salle/année/délai, ou commence/finit mal
3. Room incorrecte : ne correspond pas à la salle visible dans l'OCR
4. Year incorrecte : année mal parsée ou manquante alors qu'elle est dans l'OCR
5. Delay incorrect : délai mal parsé ou absent alors qu'il est dans l'OCR
6. Message trop long incluant la salle/année qui auraient dû être extraites séparément
7. Artefacts OCR non nettoyés dans n'importe quel champ (lettres isolées, chiffres parasites, tokens ALL-CAPS résiduels)
8. Parsing ambigu : le message contient à la fois le titre du cours ET un sous-titre séparés par "/" ou "–" qui n'ont pas été bien séparés

**CONSIGNE :** Signale toute anomalie, même si tu n'es pas sûr à 100%. Mieux vaut un faux positif qu'une erreur ignorée.

Réponds en JSON :
- Si anomalie(s) détectée(s) : {{"anomalie": "description concise de CE QUI EST FAUX et CE QUE ça DEVRAIT ÊTRE"}}
- Si tout semble correct : {{"anomalie": null}}
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
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": content}]
        )
        result_text = resp.content[0].text.strip()
        print(f"  Réponse Sonnet: {result_text[:300]}")

        result = None
        for m_json in re.finditer(r'\{', result_text):
            try:
                end = result_text.rindex('}', m_json.start()) + 1
                result = json.loads(result_text[m_json.start():end])
                break
            except Exception:
                continue

        if result is None:
            print("  ⚠️ Pas de JSON valide.")
            anomalies_run.append({"id": aid, "description": "(réponse invalide)", "fixed": False})
        elif result.get("anomalie"):
            anomalie = result["anomalie"]
            print(f"  🔴 Sonnet: {anomalie}")
            anomalies_run.append({"id": aid, "description": anomalie[:120], "fixed": False})
            create_issue(aid, ann, anomalie, ocr_log)
        else:
            print("  ✅ Aucune anomalie.")

    except Exception as e:
        print(f"  ❌ Erreur Claude: {e}")

    analyzed.add(aid)

# --- Sauvegarder IDs ---
with open(ANALYZED_FILE, "w") as f:
    f.write("\n".join(sorted(analyzed)) + "\n")

# --- Version main.py ---
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

# --- Résumé au site ---
run_summary = {
    "ts": int(datetime.datetime.utcnow().timestamp() * 1000),
    "version": version,
    "analyzed": len(nouvelles),
    "anomalies": anomalies_run,
}
try:
    r = requests.post(f"{SITE}/api/cours/watchdog-history", json=run_summary, timeout=10)
    print(f"\nRésumé envoyé ({r.status_code}).")
except Exception as e:
    print(f"\nErreur envoi résumé: {e}")

print("\nAnalyse terminée.")
sys.exit(0)
