"""Auto-fix OCR anomaly issues — appelé par GitHub Actions."""
import anthropic
import json
import os
import re
import sys

MAIN_PY = "CoursSW/main.py"

issue_number = os.environ["ISSUE_NUMBER"]
issue_title  = os.environ["ISSUE_TITLE"]
issue_body   = os.environ["ISSUE_BODY"]

# Extraire les infos clés de l'issue
def extract_field(label, text):
    m = re.search(rf'\*\*{label}\*\*\s*:?\s*`?([^`\n]+)`?', text)
    return m.group(1).strip() if m else ""

author  = extract_field("Champs parsés.*?author",  issue_body) or extract_field("author", issue_body)
room    = extract_field("room",    issue_body)
message = extract_field("message", issue_body)
year    = extract_field("year",    issue_body)
title   = extract_field("title",   issue_body)

m_ocr = re.search(r'```\s*\n(.*?)\n\s*```', issue_body, re.DOTALL)
ocr_log = m_ocr.group(1).strip() if m_ocr else ""

# Extraire la section parsing de main.py (réduit les tokens)
with open(MAIN_PY, encoding="utf-8") as f:
    main_py = f.read()

relevant_keywords = (
    "_NAME_", "_STOP", "_ROOMS", "_CAPS", "_ALL_", "VERSION",
    "def _parse", "def _extract", "def _clean", "def _best",
    "_PREPOSITIONS", "_YEAR", "_DELAY", "_SUBJECT"
)
lines = main_py.split("\n")
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
parsing_section = "\n".join(parsing_lines) if parsing_lines else main_py

prompt = f"""Tu dois corriger un bug de parsing OCR dans main.py.

Issue : {issue_title}

Champs parsés incorrectement :
- author: {author}
- title: {title}
- room: {room}
- year: {year}
- message: {message}

Texte brut OCR :
```
{ocr_log}
```

Section parsing de main.py :
```python
{parsing_section}
```

Règles :
- Corrige UNIQUEMENT le bug décrit dans l'issue.
- Modifie au minimum de code (ajouter un mot-clé dans _ROOMS, _STOP, une correction OCR, etc.)
- Ne change RIEN d'autre.
- RÈGLE ABSOLUE : si la correction n'est pas certaine, réponds {{"fix": null}}.

Réponds UNIQUEMENT en JSON :
{{"fix_old": "texte exact à remplacer dans main.py", "fix_new": "remplacement", "explanation": "explication courte"}}

Ou si pas de correction certaine :
{{"fix": null}}
"""

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
    messages=[{"role": "user", "content": prompt}]
)
result_text = resp.content[0].text.strip()
print(f"Réponse Claude: {result_text[:400]}")

# Extraire JSON
result = None
for m in re.finditer(r'\{', result_text):
    try:
        end = result_text.rindex('}', m.start()) + 1
        result = json.loads(result_text[m.start():end])
        break
    except Exception:
        continue

if not result:
    print("Pas de JSON valide.")
    sys.exit(0)

if not result.get("fix_old"):
    print("Aucune correction appliquée.")
    # On ferme quand même l'issue (anomalie connue, pas de fix code nécessaire)
    with open("fix_comment.txt", "w") as f:
        f.write("Analysé automatiquement — aucune correction de code nécessaire pour cette anomalie.")
    sys.exit(0)

fix_old = result["fix_old"]
fix_new = result["fix_new"]
explanation = result.get("explanation", "")

if fix_old not in main_py:
    print(f"fix_old introuvable dans main.py : {fix_old[:100]}")
    with open("fix_comment.txt", "w") as f:
        f.write(f"Correction automatique échouée : le texte cible n'a pas été trouvé dans main.py.\n\nCorrection proposée :\n```\n{fix_old}\n→\n{fix_new}\n```")
    sys.exit(0)

# Appliquer le fix
new_main = main_py.replace(fix_old, fix_new, 1)

# Bumper VERSION
def bump_version(content):
    m = re.search(r'VERSION\s*=\s*"([\d.]+)"', content)
    if not m:
        return content
    parts = m.group(1).split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    new_ver = ".".join(parts)
    return content.replace(m.group(0), f'VERSION = "{new_ver}"'), new_ver

result_bump = bump_version(new_main)
if isinstance(result_bump, tuple):
    new_main, new_ver = result_bump
else:
    new_main, new_ver = result_bump, "?"

with open(MAIN_PY, "w", encoding="utf-8") as f:
    f.write(new_main)

print(f"Fix appliqué → VERSION {new_ver}")

with open("fix_comment.txt", "w") as f:
    f.write(f"Corrigé automatiquement dans v{new_ver}.\n\n**Fix appliqué :** {explanation}\n```\n- {fix_old}\n+ {fix_new}\n```")

with open("new_version.txt", "w") as f:
    f.write(new_ver)
