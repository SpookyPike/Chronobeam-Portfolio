#!/usr/bin/env python3
"""Generate the browser-bundle third-party notice file from the production tree."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
TARGET = FRONTEND / "public" / "third-party-notices.txt"

raw = subprocess.run(
    ["pnpm", "licenses", "list", "--prod", "--json"],  # noqa: S607 - pnpm from PATH
    cwd=FRONTEND,
    capture_output=True,
    check=True,
    text=True,
).stdout
inventory: dict[str, list[dict]] = json.loads(raw)

lines = [
    "Chronobeam bundles the JavaScript packages below into the browser build.",
    "Each remains under its own licence. This file is generated from the",
    "installed production dependency tree.",
    "",
    "Regenerate with: pnpm licenses list --prod --json",
    "",
]
packages = sorted(
    (entry for entries in inventory.values() for entry in entries),
    key=lambda entry: entry["name"],
)
for entry in packages:
    lines.append("=" * 78)
    lines.append(f"{entry['name']} {', '.join(entry['versions'])}")
    lines.append(f"Licence: {entry['license']}")
    if entry.get("homepage"):
        lines.append(f"Homepage: {entry['homepage']}")
    lines.append("")
    licence_file = None
    for path in entry["paths"]:
        for candidate in ("LICENSE", "LICENSE.md", "LICENCE", "license"):
            possible = Path(path) / candidate
            if possible.exists():
                licence_file = possible
                break
        if licence_file:
            break
    if licence_file:
        lines.append(licence_file.read_text(encoding="utf-8", errors="replace").strip())
    else:
        lines.append("(licence text not shipped in the package; see the homepage)")
    lines.append("")

TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {TARGET} ({len(packages)} packages, {TARGET.stat().st_size} bytes)")
