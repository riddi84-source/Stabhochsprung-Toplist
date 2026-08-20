"""
Woechentlicher Scraper fuer die Stabhochsprung-Weltbestenlisten (U18, U20, Erwachsene).

WICHTIGER HINWEIS zur Altersklassen-Filterung
-----------------------------------------------
World Athletics bietet in der URL eigentlich einen Alterskategorie-Parameter an
(z.B. ".../men/u18/2026"), ABER: dieser Parameter wird von der Webseite nicht
zuverlaessig angewendet -- die Seite zeigt trotzdem die offene/absolute Bestenliste
("senior"), auch wenn "u18" angefragt wird (getestet und bestaetigt am 19./20.08.2026).
Das ist ein Verhalten der World-Athletics-Webseite selbst, kein Fehler in diesem Skript.

Deshalb geht dieser Scraper einen anderen Weg: Er laedt die offene Bestenliste
(die ohnehin ALLE Athlet:innen enthaelt) und berechnet die Altersklasse SELBST
aus dem Geburtsdatum, das in der Tabelle ohnehin mitgeliefert wird. Regel nach
World-Athletics-Konvention: Altersklasse = Alter am 31. Dezember des Wettkampfjahres.
  - U16: 14-15 Jahre
  - U18: 16-17 Jahre
  - U20: 18-19 Jahre
  - Erwachsene (senior): alles darueber

WICHTIGE EINSCHRAENKUNG: Die Bestenliste ist nach LEISTUNG sortiert, nicht nach
Alter. Juengere Athlet:innen stehen daher weiter hinten in der Liste. Um genug
U18/U20-Ergebnisse zu erfassen, blaettert der Scraper mehrere Seiten durch
(siehe PAGES_TO_FETCH unten). Mehr Seiten = mehr erfasste Jugend-Ergebnisse,
aber auch laengere Laufzeit. U16 wird realistisch kaum erfasst, weil deren
Leistungen meist so weit hinten in der absoluten Bestenliste stehen, dass sie
mit vertretbarer Seitenzahl nicht erreichbar sind.
"""

import asyncio
import json
import re
from datetime import datetime, date, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "https://worldathletics.org/records/toplists/jumps/pole-vault/all/{gender}/senior/{year}"
GENDERS = {"m": "men", "w": "women"}

# Wie viele Seiten der absoluten Bestenliste pro Geschlecht durchblaettert werden.
# Jede Seite enthaelt ca. 100 Eintraege. Hoeher = mehr U18/U20-Tiefe, aber laenger.
PAGES_TO_FETCH = 15

DATA_FILE = Path("data.json")
PREVIOUS_FILE = Path("previous_data.json")


def normalize_date(raw: str) -> str:
    try:
        d = datetime.strptime(raw.strip(), "%d %b %Y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return raw.strip()


def classify_age(dob_raw: str, mark_date_iso: str) -> str:
    """Altersklasse aus Geburtsdatum + Wettkampfdatum berechnen (WA-Konvention:
    Alter am 31. Dezember des Wettkampfjahres)."""
    try:
        dob = datetime.strptime(dob_raw.strip(), "%d %b %Y")
    except ValueError:
        return "senior"  # DOB unbekannt/leer -> als Erwachsene einordnen (konservativ)

    try:
        mark_year = int(mark_date_iso[:4])
    except (ValueError, TypeError):
        mark_year = date.today().year

    age_at_year_end = mark_year - dob.year
    if 14 <= age_at_year_end <= 15:
        return "u16"
    if 16 <= age_at_year_end <= 17:
        return "u18"
    if 18 <= age_at_year_end <= 19:
        return "u20"
    return "senior"


async def fetch_page(context, gender: str, year: int, page_num: int):
    url = (BASE_URL.format(gender=GENDERS[gender], year=year) +
           f"?regionType=world&windReading=regular&page={page_num}&bestResultsOnly=true")
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            await page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            return []  # z.B. letzte Seite ohne weitere Eintraege

        rows = await page.query_selector_all("table tbody tr")
        results = []
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 9:
                continue
            texts = [await c.inner_text() for c in cells]

            try:
                mark = float(texts[1].strip().replace(",", "."))
            except ValueError:
                continue

            name = texts[3].strip().split("\n")[0]
            dob_raw = texts[4].strip() if len(texts) > 4 else ""
            country_match = re.search(r"\b([A-Z]{3})\b", texts[5]) if len(texts) > 5 else None
            mark_date = normalize_date(texts[-2] if len(texts) >= 9 else texts[-1])
            venue = texts[-3] if len(texts) >= 9 else ""

            results.append({
                "name": name,
                "country": country_match.group(1) if country_match else "",
                "gender": gender,
                "ageClass": classify_age(dob_raw, mark_date),
                "mark": mark,
                "unit": "m",
                "date": mark_date,
                "venue": venue,
            })
        return results
    finally:
        await page.close()


async def fetch_gender_toplist(browser, gender: str, year: int, pages: int):
    context = await browser.new_context()
    all_rows = []
    try:
        for p in range(1, pages + 1):
            rows = await fetch_page(context, gender, year, p)
            if not rows:
                print(f"  -> Seite {p}: keine weiteren Eintraege, Abbruch")
                break
            all_rows.extend(rows)
            print(f"  -> Seite {p}: {len(rows)} Eintraege")
    finally:
        await context.close()
    return all_rows


def entry_key(e: dict) -> str:
    """Eindeutiger Schluessel pro Leistung, fuer den Wochenvergleich."""
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

        for gender in GENDERS:
            print(f"Lade {gender} / senior (offene Liste, {PAGES_TO_FETCH} Seiten) / {year} ...")
            rows = await fetch_gender_toplist(browser, gender, year, PAGES_TO_FETCH)
            all_results.extend(rows)

        await browser.close()

    # Diagnose: wie viele Eintraege pro Altersklasse/Geschlecht wurden gefunden?
    print("\nAufschluesselung nach Altersklasse:")
    for gender in GENDERS:
        for age_class in ["u16", "u18", "u20", "senior"]:
            count = sum(1 for e in all_results if e["gender"] == gender and e["ageClass"] == age_class)
            print(f"  {gender} / {age_class}: {count} Eintraege")

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
    # naechsten Lauf (naechste Woche) als Vergleichsbasis existiert.
    PREVIOUS_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    new_count = sum(1 for e in all_results if e["isNew"])
    print(f"\nFertig: {len(all_results)} Eintraege gesamt, davon {new_count} neu seit letzter Woche.")
    return all_results, new_count


if __name__ == "__main__":
    asyncio.run(run())
