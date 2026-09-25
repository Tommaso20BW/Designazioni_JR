import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROME       = ZoneInfo("Europe/Rome")
BASE_DIR   = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "designazioni.json"

TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TIMEOUT = 15   # ridotto da 30
RETRIES = 2    # ridotto da 4

UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139--juventus/matches/",
    "Europa League":    "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    # fix: mancava .com nel dominio
    "Conference League": "https://it.uefa.com/uefaeuropaconferenceleague/clubs/50139--juventus/matches/",
}

AIA      = "https://www.aia-figc.it/news/?c=9"
AIA_HOST = "www.aia-figc.it"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
})

ROLES = {
    "ARBITRO":    ["arbitro", "referee"],
    "ASSISTENTI": ["assistenti", "assistant referees", "assistant referee"],
    "IV":         ["iv", "quarto ufficiale", "fourth official"],
    "VAR":        ["var"],
    "AVAR":       ["avar"],
}

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


# ── utilità ───────────────────────────────────────────────────────────────────

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def request(url: str) -> requests.Response:
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            print(f"[HTTP] {attempt}/{RETRIES} {url}")
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            print(f"[HTTP] errore: {exc}")
            if attempt < RETRIES:
                time.sleep(attempt * 2)   # 2s invece di 4s×attempt
    raise RuntimeError(f"richiesta fallita: {url} -> {last}")


# ── stato ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"uefa": {}, "italia": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("uefa", {})
        data.setdefault("italia", {})
        return data
    except Exception:
        return {"uefa": {}, "italia": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


# ── telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.")
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(str(r.json()))


# ── parsing ───────────────────────────────────────────────────────────────────

def parse_date(text: str) -> str | None:
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", text, re.I)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def extract_roles(soup: BeautifulSoup) -> dict:
    text = clean(soup.get_text(" ", strip=True))
    result: dict[str, str] = {}
    boundary = "|".join(re.escape(x) for v in ROLES.values() for x in v)

    for role, aliases in ROLES.items():
        for alias in aliases:
            pat = rf"\b{re.escape(alias)}\b\s*[:\-]?\s*(.+?)(?=\s+(?:{boundary})\b|$)"
            m = re.search(pat, text, re.I)
            if m:
                val = clean(m.group(1)).strip(":- ")
                if val:
                    result[role] = val
                    break

    for tag in soup.find_all(["li", "p", "div", "span", "td"]):
        val = clean(tag.get_text(" ", strip=True))
        if not val or len(val) > 300:
            continue
        for role, aliases in ROLES.items():
            if role in result:
                continue
            for alias in aliases:
                m = re.match(rf"^{re.escape(alias)}\s*[:\-]\s*(.+)$", val, re.I)
                if m:
                    result[role] = clean(m.group(1))
                    break

    return result


def title_from_soup(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["h1", "h2", "title"]):
        val = clean(tag.get_text(" ", strip=True))
        if "juventus" in val.lower() and len(val) < 180:
            return val
    return "Juventus"


def hashtag(title: str) -> str:
    title = re.sub(r"\b(?:vs|v)\b", "", title, flags=re.I)
    title = re.sub(r"[^A-Za-z0-9À-ÖØ-öø-ÿ]", "", title)
    return title or "Juve"


def format_message(prefix: str, title: str, roles: dict) -> str:
    lines = [f"{prefix} Designazione arbitrale di #{hashtag(title)}:", ""]
    for role in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
        if roles.get(role):
            lines.append(f"{role}: {roles[role]}")
    return "\n".join(lines)


# ── UEFA ──────────────────────────────────────────────────────────────────────

def match_id(url: str) -> str | None:
    m = re.search(r"/match/(\d+)", url)
    return m.group(1) if m else None


def find_match_links(soup: BeautifulSoup) -> list[str]:
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/match/" not in href:
            continue
        mid = match_id(href)
        if not mid:
            continue
        href = href.split("?", 1)[0].split("#", 1)[0]
        if "/matchinfo" not in href:
            href = href.rstrip("/") + "/matchinfo/"
        found.add(urljoin("https://it.uefa.com", href))
    return sorted(found)


def check_uefa(state: dict) -> bool:
    changed = False
    for competition, calendar in UEFA.items():
        try:
            soup = BeautifulSoup(request(calendar).text, "html.parser")
        except Exception as exc:
            print(f"[UEFA] {competition}: {exc}")
            continue

        links = find_match_links(soup)
        print(f"[UEFA] {competition}: {len(links)} partite trovate")

        for url in links:
            mid = match_id(url)
            if not mid:
                continue
            key = f"{competition}:{mid}"
            if key in state["uefa"]:
                print(f"[SKIP] {key}")
                continue

            try:
                page = BeautifulSoup(request(url).text, "html.parser")
            except Exception as exc:
                print(f"[UEFA] match {mid}: {exc}")
                continue

            text = clean(page.get_text(" ", strip=True))
            if "juventus" not in text.lower():
                continue

            roles = extract_roles(page)
            if not roles.get("ARBITRO"):
                continue

            title   = title_from_soup(page)
            message = format_message("🇪🇺ℹ️", title, roles)

            try:
                send_telegram(message)
            except Exception as exc:
                print(f"[UEFA] Telegram: {exc}")
                continue

            state["uefa"][key] = {
                "competition": competition,
                "match_id":    mid,
                "match":       title,
                "url":         url,
                "sent_at":     datetime.now(ROME).isoformat(),
            }
            save_state(state)
            changed = True
            print(f"[SENT] {key}")

    return changed


# ── AIA Italia ────────────────────────────────────────────────────────────────

def article_links(soup: BeautifulSoup) -> list[str]:
    """
    Solo link che sembrano articoli reali su aia-figc.it:
    - stesso host
    - path con almeno 2 segmenti (es. /news/12345/)
    - nessun query string (esclude ?c=9 e simili)
    - anchor non vuoto
    - anchor o href contiene "juventus" o "juve" (evita di scaricare articoli irrilevanti)
    """
    seen: set[str] = set()
    result: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue

        full   = urljoin(AIA, href)
        parsed = urlparse(full)

        if parsed.netloc != AIA_HOST:
            continue
        if parsed.query:
            continue   # esclude ?c=9 ecc.

        segments = [s for s in parsed.path.rstrip("/").split("/") if s]
        if len(segments) < 2:
            continue   # esclude /news/ e pagine di primo livello

        anchor = clean(a.get_text(" ", strip=True))
        if not anchor:
            continue

        # pre-filtro: segui solo link che già menzionano Juventus
        needle = anchor.lower() + href.lower()
        if "juventus" not in needle and "juve" not in needle:
            continue

        if full not in seen:
            seen.add(full)
            result.append(full)

    return result


def check_italia(state: dict) -> bool:
    today = datetime.now(ROME).date().isoformat()

    try:
        index_soup = BeautifulSoup(request(AIA).text, "html.parser")
    except Exception as exc:
        print(f"[ITALIA] AIA index: {exc}")
        return False

    links = article_links(index_soup)
    print(f"[ITALIA] {len(links)} articoli Juventus trovati nell'indice")

    changed = False
    for url in links:
        key = f"{today}:{url}"
        if key in state["italia"]:
            print(f"[SKIP] {key}")
            continue

        try:
            soup = BeautifulSoup(request(url).text, "html.parser")
        except Exception as exc:
            print(f"[ITALIA] {url}: {exc}")
            continue

        text = clean(soup.get_text(" ", strip=True))
        if parse_date(text) != today:
            continue
        if "juventus" not in text.lower():
            continue

        roles = extract_roles(soup)
        if not roles.get("ARBITRO"):
            continue

        title   = title_from_soup(soup)
        message = format_message("🇮🇹ℹ️", title, roles)

        try:
            send_telegram(message)
        except Exception as exc:
            print(f"[ITALIA] Telegram: {exc}")
            continue

        state["italia"][key] = {
            "date":    today,
            "match":   title,
            "url":     url,
            "sent_at": datetime.now(ROME).isoformat(),
        }
        save_state(state)
        changed = True
        print(f"[SENT] {key}")

    return changed


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    state = load_state()
    print(f"[START] {datetime.now(ROME).isoformat()}")
    check_uefa(state)
    check_italia(state)
    print("[DONE] Controllo completato.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRORE: {exc}")
        sys.exit(1)
