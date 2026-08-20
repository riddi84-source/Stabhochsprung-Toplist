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


async def fetch_toplist(browser, gender: str, age_class: str, year: int, limit_rows: int = 100):
    # Wichtig: pro Kategorie ein FRISCHER Browser-Kontext (wie ein neues Inkognito-Fenster).
    # Grund: worldathletics.org ist eine Single-Page-App mit internem Daten-Cache. Bei
    # Wiederverwendung derselben Seite über mehrere Kategorien hinweg (z.B. u18 -> u20 ->
    # senior) kann die App zwischengespeicherte Daten der vorherigen Kategorie anzeigen,
    # statt neu zu laden. Ein neuer Kontext pro Anfrage vermeidet das zuverlässig.
    context = await browser.new_context()
    page = await context.new_page()
    try:
        url = BASE_URL.format(gender=GENDERS[gender], age=AGE_CLASSES[age_class], year=year)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_selector("table tbody tr", timeout=15000)

        # Sicherheitscheck: steht die erwartete Kategorie im Seitentitel? Falls nicht,
        # hat die Seite vermutlich nicht die richtige Kategorie geladen.
        title = await page.title()
        expected = f"{GENDERS[gender]} - {AGE_CLASSES[age_class]}"
        if expected not in title.lower().replace("_", " "):
            print(f"  -> WARNUNG: Seitentitel '{title}' passt nicht zu erwarteter Kategorie '{expected}'")

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
    finally:
        await context.close()


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
    marks_seen_per_key = {}  # (gender) -> {age_class: set of (name, mark)} für Duplikat-Check

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for gender in GENDERS:
            marks_seen_per_key[gender] = {}
            for age_class in AGE_CLASSES:
                print(f"Lade {gender} / {age_class} / {year} ...")
                try:
                    rows = await fetch_toplist(browser, gender, age_class, year)
                    all_results.extend(rows)
                    print(f"  -> {len(rows)} Einträge")
                    marks_seen_per_key[gender][age_class] = {(r["name"], r["mark"]) for r in rows}
                except Exception as e:
                    print(f"  -> FEHLER bei {gender}/{age_class}: {e}")

        await browser.close()

    # Duplikat-Check: warnt, falls zwei Kategorien verdächtig identische Top-Ergebnisse
    # liefern (Hinweis darauf, dass die Filterung nicht gegriffen hat)
    for gender, per_age in marks_seen_per_key.items():
        ages = list(per_age.keys())
        for i in range(len(ages)):
            for j in range(i + 1, len(ages)):
                a, b = ages[i], ages[j]
                overlap = per_age[a] & per_age[b]
                if len(overlap) >= min(len(per_age[a]), len(per_age[b])) * 0.8 and overlap:
                    print(f"WARNUNG: {gender}/{a} und {gender}/{b} liefern zu ~{len(overlap)} identische "
                          f"Top-Einträge — moeglicherweise hat die Alterskategorie-Filterung nicht gegriffen.")

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
