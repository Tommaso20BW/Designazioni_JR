import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROME = ZoneInfo("Europe/Rome")
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "designazioni.json"

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TIMEOUT = 30
RETRIES = 4
MAX_ARTICLES = 3  # quante news "designazioni" recenti controllare ad ogni run

UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    # FIX: mancava ".com" e il segmento "uefa" nel dominio
    "Conference League": "https://it.uefa.com/uefaconferenceleague/clubs/50139--juventus/matches/",
}

AIA = "https://www.aia-figc.it/news/?c=9"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
})

ROLES = {
    "ARBITRO": ["arbitro", "referee"],
    "ASSISTENTI": ["assistenti", "assistant referees", "assistant referee"],
    "IV": ["iv", "quarto ufficiale", "fourth official"],
    "VAR": ["var"],
    "AVAR": ["avar"],
}

ROLE_ORDER = ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def request(url):
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
                time.sleep(attempt * 4)
    raise RuntimeError(f"richiesta fallita: {url} -> {last}")


def load_state():
    if not STATE_FILE.exists():
        return {"uefa": {}, "italia": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("uefa", {})
        data.setdefault("italia", {})
        return data
    except Exception:
        return {"uefa": {}, "italia": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.")
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(str(r.json()))


# --------------------------------------------------------------------------
# Validazione: evita che un parsing sbagliato venga pubblicato
# --------------------------------------------------------------------------

TIME_LIKE = re.compile(r"(^h\.?\s*\d)|(\b\d{1,2}[.:]\d{2}\b)", re.I)


def valid_roles(roles):
    """True solo se c'è almeno l'arbitro e nessun valore sembra un orario/data."""
    if not roles.get("ARBITRO"):
        return False
    for value in roles.values():
        if TIME_LIKE.search(value) or re.search(r"\d{1,2}/\d{1,2}", value):
            return False
    return True


def format_message(prefix, tag, roles):
    lines = [f"{prefix} Designazione arbitrale di #{html.escape(tag)}:", ""]
    for role in ROLE_ORDER:
        if roles.get(role):
            lines.append(f"{role}: <b>{html.escape(roles[role])}</b>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# UEFA (parser originale a etichette: lì il formato è "Arbitro: Nome")
# --------------------------------------------------------------------------

def match_id(url):
    m = re.search(r"/match/(\d+)", url)
    return m.group(1) if m else None


def find_match_links(soup):
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


def extract_roles(soup):
    text = clean(soup.get_text(" ", strip=True))
    result = {}
    labels = []
    for values in ROLES.values():
        labels.extend(values)
    boundary = "|".join(re.escape(x) for x in labels)
    for role, aliases in ROLES.items():
        for alias in aliases:
            pattern = rf"\b{re.escape(alias)}\b\s*[:\-]?\s*(.+?)(?=\s+(?:{boundary})\b|$)"
            m = re.search(pattern, text, re.I)
            if m:
                value = clean(m.group(1)).strip(":- ")
                if value:
                    result[role] = value
                    break
    for tag in soup.find_all(["li", "p", "div", "span", "td"]):
        value = clean(tag.get_text(" ", strip=True))
        if not value or len(value) > 300:
            continue
        for role, aliases in ROLES.items():
            if role in result:
                continue
            for alias in aliases:
                m = re.match(rf"^{re.escape(alias)}\s*[:\-]\s*(.+)$", value, re.I)
                if m:
                    result[role] = clean(m.group(1))
                    break
    return result


def title_from_soup(soup):
    for tag in soup.find_all(["h1", "h2", "title"]):
        value = clean(tag.get_text(" ", strip=True))
        if "juventus" in value.lower() and len(value) < 180:
            return value
    return "Juventus"


def hashtag(title):
    title = re.sub(r"\b(?:vs|v)\b", "", title, flags=re.I)
    title = re.sub(r"[^A-Za-z0-9À-ÖØ-öø-ÿ]", "", title)
    return title or "Juve"


def check_uefa(state):
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
            if not valid_roles(roles):
                print(f"[UEFA] {key}: ruoli non validi/mancanti, salto -> {roles}")
                continue
            title = title_from_soup(page)
            message = format_message("🇪🇺ℹ️", hashtag(title), roles)
            try:
                send_telegram(message)
            except Exception as exc:
                print(f"[UEFA] Telegram: {exc}")
                continue
            state["uefa"][key] = {
                "competition": competition,
                "match_id": mid,
                "match": title,
                "url": url,
                "sent_at": datetime.now(ROME).isoformat(),
            }
            save_state(state)
            changed = True
            print(f"[SENT] {key}")
    return changed


# --------------------------------------------------------------------------
# ITALIA (AIA): ogni partita è un blocco di <p>
#
#   <p><strong>LECCE – JUVENTUS</strong><em>Domenica 25/10 h. 18.00</em></p>
#   <p>MARESCA</p>                      <- arbitro (senza etichetta)
#   <p>MONDIN - BARONE</p>              <- assistenti (senza etichetta)
#   <p>IV: FELICIANI</p>
#   <p>VAR: PATERNA</p>
#   <p>AVAR: BARONI</p>
# --------------------------------------------------------------------------

HEADER_RE = re.compile(
    r"^(?P<home>.+?)\s+[–—-]\s+(?P<away>.+?)\s+"
    r"(?P<day>[A-Za-zÀ-ÿ]+)\s+(?P<date>\d{1,2}/\d{1,2})\s+"
    r"h\.?\s*(?P<time>\d{1,2}[.:]\d{2})$",
    re.I,
)
LABEL_RE = re.compile(r"^(IV|VAR|AVAR)\s*:\s*(.+)$", re.I)

SHORT_TEAM = {"JUVENTUS": "Juve"}


def person(s):
    s = re.sub(r"\(.*?\)", "", s)  # toglie "(foto)"
    return clean(s).title()


def assistants(s):
    parts = [person(x) for x in re.split(r"\s+[–—-]\s+", s) if x.strip()]
    return " - ".join(parts)


def tag_name(team):
    team = clean(team)
    if team.upper() in SHORT_TEAM:
        return SHORT_TEAM[team.upper()]
    return re.sub(r"[^A-Za-z0-9À-ÿ]", "", team.title())


def leaf_paragraphs(root):
    # il sito annida <p> dentro <p>: teniamo solo quelli "foglia"
    return [p for p in root.find_all("p") if not p.find("p")]


def parse_aia_matches(soup):
    root = soup.select_one("div.overflow-hidden") or soup
    matches = []
    current = None
    for p in leaf_paragraphs(root):
        line = clean(p.get_text(" ", strip=True))
        if not line:
            continue
        m = HEADER_RE.match(line)
        if m:
            current = {
                "home": clean(m.group("home")),
                "away": clean(m.group("away")),
                "date": m.group("date"),
                "time": m.group("time"),
                "roles": {},
            }
            matches.append(current)
            continue
        if current is None:
            continue
        roles = current["roles"]
        lab = LABEL_RE.match(line)
        if lab:
            roles[lab.group(1).upper()] = person(lab.group(2))
        elif "ARBITRO" not in roles:
            roles["ARBITRO"] = person(line)
        elif "ASSISTENTI" not in roles:
            roles["ASSISTENTI"] = assistants(line)
    return matches


def aia_article_links(soup):
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "/news/" in href and "designazioni" in href and "giornata" in href:
            links.append(urljoin(AIA, a["href"]))
    return list(dict.fromkeys(links))[:MAX_ARTICLES]


def article_id(url):
    m = re.search(r"-(\d+)/?$", url.split("?", 1)[0])
    return m.group(1) if m else url


def check_italia(state):
    try:
        index = BeautifulSoup(request(AIA).text, "html.parser")
    except Exception as exc:
        print(f"[ITALIA] AIA: {exc}")
        return False

    links = aia_article_links(index)
    print(f"[ITALIA] {len(links)} articoli designazioni da controllare")
    changed = False

    for url in links:
        try:
            page = BeautifulSoup(request(url).text, "html.parser")
        except Exception as exc:
            print(f"[ITALIA] {url}: {exc}")
            continue

        for m in parse_aia_matches(page):
            if "JUVENTUS" not in (m["home"].upper(), m["away"].upper()):
                continue

            roles = m["roles"]
            label = f"{m['home']} - {m['away']}"
            if not valid_roles(roles):
                print(f"[ITALIA] {label}: ruoli non validi, salto -> {roles}")
                continue

            key = f"{article_id(url)}:{m['home'].upper()}-{m['away'].upper()}"
            prev = state["italia"].get(key)
            if prev and prev.get("roles") == roles:
                print(f"[SKIP] {key}")
                continue

            prefix = "🇮🇹ℹ️" if not prev else "🔄🇮🇹ℹ️"  # 🔄 = designazione cambiata
            tag = tag_name(m["home"]) + tag_name(m["away"])
            message = format_message(prefix, tag, roles)
            try:
                send_telegram(message)
            except Exception as exc:
                print(f"[ITALIA] Telegram: {exc}")
                continue

            state["italia"][key] = {
                "match": label,
                "date": m["date"],
                "time": m["time"],
                "roles": roles,
                "url": url,
                "sent_at": datetime.now(ROME).isoformat(),
            }
            save_state(state)
            changed = True
            print(f"[SENT] {key}")
    return changed


def main():
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
