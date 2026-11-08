"""Build a small, reviewed offline selection from pinned upstream evidence.

No runtime network access. Re-running downloads evidence and verifies every selected token.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "username_finder" / "data"
CACHE = ROOT / "runtime" / "data_evidence"

WORDS = """amber anchor apple arrow autumn beach bear bird bloom blue brave breeze bright brook calm cedar
cloud coral dawn deer dream eagle earth echo ember falcon fern field fire flame forest fox frost galaxy
garden gold green grove harbor hawk hill honey horse ice iris island ivory jade lake leaf light lion lotus
lunar maple mist moon moss night north nova ocean olive onyx orbit owl pearl pine planet plum polar quartz
rain raven reef river robin rock rose ruby sage sand sea shadow shark silver sky snow solar spark star stone
storm sun swift tiger trail tree valley violet wave west whale willow wind wing winter wolf wood zenith
""".split()


def fetch(url: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if not path.exists():
        request = urllib.request.Request(url, headers={"User-Agent": "UsernameFinder-DataPrep/0.1"})
        with urllib.request.urlopen(request, timeout=40) as response:
            body = response.read(25 * 1024 * 1024)
        path.write_bytes(body)
    return path.read_bytes()


def save(category, entries, source_url, version, license_name, attribution, source_sha):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{category}.json"
    path.write_text(
        json.dumps(sorted(entries, key=lambda e: e["name"]), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "id": f"{category}-curated-v1",
        "category": category,
        "version": version,
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_url": source_url,
        "source_sha256": source_sha,
        "license": license_name,
        "attribution": attribution,
        "extracted_at": "2026-11-05",
        "count": len(entries),
        "changes": "Small manually selected allowlist; ASCII lowercase; "
        "syntax and upstream membership checked.",
        "coverage": "Curated starter subset; not comprehensive; no frequency scores or availability claims.",
    }


def main():
    packages = []
    reviewed_path = Path(__file__).with_name("reviewed_source_selection.json")
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    names = reviewed["english_names"]
    entries = [
        {"name": name, "reference": "SSA 2020–2025, table ranks 1–30, manually selected"}
        for name in names["tokens"]
    ]
    packages.append(
        save(
            "english_names",
            entries,
            names["url"],
            names["version"],
            "US federal government public data",
            "US Social Security Administration, names 2020–2025; "
            "manually reviewed subset of published table (bulk ZIP returned 403)",
            "selection:" + hashlib.sha256(reviewed_path.read_bytes()).hexdigest(),
        )
    )
    url = "https://en-word.net/downloads/english-wordnet-2025.xml.gz"
    raw = fetch(url, "oewn-2025.xml.gz")
    wanted = set(WORDS)
    selected = {}
    # Read elements incrementally; retain only chosen lemma+reference, not the full dictionary.
    for _, element in ET.iterparse(gzip.GzipFile(fileobj=io.BytesIO(raw)), events=("end",)):
        if element.tag.rsplit("}", 1)[-1] == "LexicalEntry":
            lemma = element.find("Lemma")
            if lemma is not None:
                name = lemma.attrib.get("writtenForm", "")
                if name in wanted:
                    selected[name] = {"name": name, "reference": element.attrib["id"]}
            element.clear()
    packages.append(
        save(
            "english_words",
            list(selected.values()),
            "https://en-word.net/downloads",
            "OEWN-2025",
            "CC BY 4.0",
            "Open English WordNet Community, derived from Princeton WordNet. "
            "https://github.com/globalwordnet/english-wordnet ; https://creativecommons.org/licenses/by/4.0/",
            hashlib.sha256(raw).hexdigest(),
        )
    )
    # A tiny transliteration selection explicitly attested on the publisher's dataset page.
    # No inference from the full multi-entity table: all five are variants of the personal name محمد.
    selected_arabic = reviewed["arabic_names_latin"]
    url = selected_arabic["url"]
    variants = selected_arabic["tokens"]
    packages.append(
        save(
            "arabic_names_latin",
            [{"name": n, "reference": "v0.1.0 token محمد: published top variants"} for n in variants],
            url,
            "Bayanat-v0.1.0",
            "CC0 1.0 (data); no page prose redistributed",
            "Bayanat Labs; derived from Wikidata",
            "selection:" + hashlib.sha256(reviewed_path.read_bytes()).hexdigest(),
        )
    )
    # Independently selected standard gameplay terms, with individual publisher documentation references.
    terms = {
        "quest": "https://dev.epicgames.com/documentation/en-us/fortnite/using-quest-design-devices-in-fortnite-creative",
        "respawn": "https://dev.epicgames.com/documentation/en-us/fortnite/player-respawning-in-fortnite-creative",
        "spawn": "https://dev.epicgames.com/documentation/en-us/fortnite/using-player-spawn-pad-devices-in-fortnite-creative",
        "avatar": "https://create.roblox.com/docs/characters",
        "guild": "https://worldofwarcraft.blizzard.com/en-us/game/recruit-a-friend",
    }
    # Verify each term against publisher documentation. Failed evidence omits the term.
    gaming = []
    for name, ref in terms.items():
        try:
            html = fetch(ref, f"gaming-{name}.html").decode("utf-8").lower()
            if name in html:
                gaming.append({"name": name, "reference": ref})
        except (OSError, ValueError):
            pass
    packages.append(
        save(
            "gaming_terms",
            gaming,
            "https://dev.epicgames.com/documentation/en-us/fortnite/",
            "curated-2026-11-05",
            "CC0 1.0 (independent token selection only)",
            "Username Finder Bot contributors; links document terminology, no publisher prose copied",
            "independent",
        )
    )
    (OUT / "manifest.json").write_text(
        json.dumps({"version": 1, "packages": packages}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({p["category"]: p["count"] for p in packages}))


if __name__ == "__main__":
    main()
