"""Met à jour Almanach-PEH après un fix OCR (version, patchnote, liens)."""
import re
import os
import datetime
import sys

if not os.path.exists("fix_info.txt"):
    sys.exit(0)

with open("fix_info.txt") as f:
    lines = f.read().strip().split("\n")
new_ver = lines[0]
fixes = lines[1] if len(lines) > 1 else "correction OCR"

# --- COURS_REQUIRED_VERSION ---
for root, dirs, files in os.walk("Almanach-PEH/src"):
    for fname in files:
        if not fname.endswith((".ts", ".tsx", ".js")):
            continue
        fpath = os.path.join(root, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if "COURS_REQUIRED_VERSION" not in content:
            continue
        new_content = re.sub(
            r"(COURS_REQUIRED_VERSION\s*=\s*['\"])[\d.]+",
            lambda m: m.group(1) + new_ver,
            content
        )
        if new_content != content:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"COURS_REQUIRED_VERSION -> {new_ver} dans {fpath}")

# --- CoursSection.tsx : liens ---
cours_section = "Almanach-PEH/src/components/cours/CoursSection.tsx"
with open(cours_section, encoding="utf-8") as f:
    content = f.read()
content = re.sub(r'releases/download/v[\d.]+/CourSW\.zip', f'releases/download/v{new_ver}/CourSW.zip', content)
content = re.sub(r'⬇️ v[\d.]+', f'⬇️ v{new_ver}', content)
with open(cours_section, "w", encoding="utf-8") as f:
    f.write(content)
print(f"CoursSection.tsx liens -> v{new_ver}")

# --- patchnotes.ts ---
patchnotes = "Almanach-PEH/src/data/patchnotes.ts"
with open(patchnotes, encoding="utf-8") as f:
    pn_content = f.read()

ver_match = re.search(r"version:\s*'v([\d.]+)'", pn_content)
if ver_match:
    old_pn_ver = ver_match.group(1)
    pn_parts = old_pn_ver.split(".")
    pn_parts[-1] = str(int(pn_parts[-1]) + 1)
    new_pn_ver = ".".join(pn_parts)
    today = datetime.date.today().strftime("%d/%m/%Y")
    new_entry = (
        f"  {{\n"
        f"    version: 'v{new_pn_ver}',\n"
        f"    date: '{today}',\n"
        f"    changes: [\n"
        f"      'fix OCR : {fixes} (CourSW v{new_ver})',\n"
        f"    ],\n"
        f"  }},\n"
    )
    pn_content = re.sub(
        r'(export const patchnotes\s*[=:][^[]*\[)',
        r'\1\n' + new_entry,
        pn_content,
        count=1
    )
    with open(patchnotes, "w", encoding="utf-8") as f:
        f.write(pn_content)
    print(f"patchnotes.ts: v{old_pn_ver} -> v{new_pn_ver}")
