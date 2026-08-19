"""
Wöchentlicher Scraper für die Stabhochsprung-Weltbestenlisten (U18, U20, Erwachsene).

Ablauf:
  1. Rendert die World-Athletics-Toplists mit einem echten Browser (Playwright),
     weil die Tabellen dort per JavaScript nachgeladen werden.
  2. Vergleicht das Ergebnis mit der Vorwoche (previous_data.json) und markiert
     neue Einträge mit isNew=true.
  3. Schreibt data.json — genau das Format, das dashboard.html per fetch() lädt.
  4. Hebt die aktuelle data.json zu previous_data.json auf, für den nächsten Lauf.

U16 ist hier NICHT enthalten, weil World Athletics diese Altersklasse nicht führt.
Siehe ANLEITUNG.md, Abschnitt "Erweiterungen", falls du das später ergänzen willst.
"""

import asyncio
import json
import re
from datetime import datetime, date, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "https://worldathletics.org/records/toplists/jumps/pole-vault/all/{gender}/{age}/{year}"
GENDERS = {"m": "men", "w": "women"}
AGE_CLASSES = {"u18": "u18", "u20": "u20", "senior": "senior"}

DATA_FILE = Path("data.json")
PREVIOUS_FILE = Path("previous_data.json")


async def fetch_toplist(page, gender: str, age_class: str, year: int, limit_rows: int = 100):
    url = BASE_URL.format(gender=GENDERS[gender], age=AGE_CLASSES[age_class], year=year)
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_selector("table tbody tr", timeout=15000)

    rows = await page.query_selector_all("table tbody tr")
    results = []
    for row in rows[:limit_rows]:
        cells = await row.query_selector_all("td")
        if len(cells) < 8:
            continue
        texts = [await c.inner_text() for c in cells]

        try:
            mark = float(texts[1].strip().replace(",", "."))
        except ValueError:
            continue

        name = texts[3].strip().split("\n")[0]
        country_match = re.search(r"\b([A-Z]{3})\b", texts[5]) if len(texts) > 5 else None

        results.append({
            "name": name,
            "country": country_match.group(1) if country_match else "",
            "gender": gender,
            "ageClass": age_class,
            "mark": mark,
            "unit": "m",
            "date": normalize_date(texts[-2] if len(texts) >= 9 else texts[-1]),
            "venue": texts[-3] if len(texts) >= 9 else "",
        })
    return results


def normalize_date(raw: str) -> str:
    try:
        d = datetime.strptime(raw.strip(), "%d %b %Y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return raw.strip()


def entry_key(e: dict) -> str:
    """Eindeutiger Schlüssel pro Leistung, für den Wochenvergleich."""
    return f"{e['name']}|{e['ageClass']}|{e['gender']}|{e['mark']}|{e['date']}"


def mark_new_entries(current: list[dict], previous: list[dict]) -> list[dict]:
    previous_keys = {entry_key(e) for e in previous}
    for e in current:
        e["isNew"] = entry_key(e) not in previous_keys
    return current


async def run(year: int | None = None):
    year = year or date.today().year
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for gender in GENDERS:
            for age_class in AGE_CLASSES:
                print(f"Lade {gender} / {age_class} / {year} ...")
                try:
                    rows = await fetch_toplist(page, gender, age_class, year)
                    all_results.extend(rows)
                    print(f"  -> {len(rows)} Einträge")
                except Exception as e:
                    print(f"  -> FEHLER bei {gender}/{age_class}: {e}")

        await browser.close()

    # Diff gegen Vorwoche
    previous = []
    if PREVIOUS_FILE.exists():
        try:
            prev_json = json.loads(PREVIOUS_FILE.read_text(encoding="utf-8"))
            previous = prev_json.get("entries", prev_json)
        except Exception:
            previous = []

    all_results = mark_new_entries(all_results, previous)

    output = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "entries": all_results,
    }
    DATA_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    # previous_data.json wird JETZT auf den Stand von heute gesetzt, damit sie beim
    # naechsten Lauf (naechste Woche) als Vergleichsbasis existiert. So muss der
    # Workflow nie pruefen, ob die Datei schon existiert -- sie ist nach jedem
    # erfolgreichen Lauf garantiert vorhanden.
    PREVIOUS_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    new_count = sum(1 for e in all_results if e["isNew"])
    print(f"\nFertig: {len(all_results)} Einträge gesamt, davon {new_count} neu seit letzter Woche.")
    return all_results, new_count


if __name__ == "__main__":
    asyncio.run(run())
