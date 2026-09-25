import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = DATA_DIR / "designazioni.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )
}


ROLES = {
    "ARBITRO": [
        "Arbitro",
        "Arbitro principale",
        "Referee",
    ],
    "ASSISTENTI": [
        "Assistenti arbitrali",
        "Assistenti",
        "Assistant referees",
    ],
    "IV": [
        "Quarto uomo",
        "Quarto arbitro",
        "Fourth official",
    ],
    "VAR": [
        "Video Assistant Referee",
        "VAR",
    ],
    "AVAR": [
        "Assistente Video Assistant Referee",
        "Assistant Video Assistant Referee",
        "AVAR",
    ],
}


# ============================================================
# UTILITY
# ============================================================

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}

    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as exc:
        print(f"[STORICO] Impossibile leggere lo storico: {exc}")

    return {}


def save_history(history: dict) -> None:
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    response.raise_for_status()


# ============================================================
# SCRAPING
# ============================================================

def fetch_page(url: str) -> BeautifulSoup:
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=45,
    )

    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


def extract_roles(soup: BeautifulSoup) -> dict:
    text = clean(soup.get_text(" ", strip=True))
    result: dict[str, str] = {}

    boundary = "|".join(
        re.escape(x)
        for values in ROLES.values()
        for x in values
    )

    # Primo tentativo: ricerca sull'intero testo della pagina.
    for role, aliases in ROLES.items():

        for alias in aliases:

            pattern = (
                rf"\b{re.escape(alias)}\b"
                rf"\s*[:\-]?\s*(.+?)"
                rf"(?=\s+(?:{boundary})\b|$)"
            )

            match = re.search(
                pattern,
                text,
                re.I,
            )

            if match:
                value = clean(
                    match.group(1)
                ).strip(":- ")

                if value:
                    result[role] = value
                    break

    # Secondo tentativo: ricerca nei singoli elementi HTML.
    for tag in soup.find_all(
        ["li", "p", "div", "span", "td"]
    ):

        value = clean(
            tag.get_text(" ", strip=True)
        )

        if not value or len(value) > 300:
            continue

        for role, aliases in ROLES.items():

            if role in result:
                continue

            for alias in aliases:

                match = re.match(
                    rf"^{re.escape(alias)}\s*[:\-]\s*(.+)$",
                    value,
                    re.I,
                )

                if match:
                    result[role] = clean(
                        match.group(1)
                    )
                    break

    return result


def title_from_soup(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(
        ["h1", "h2", "title"]
    ):

        value = clean(
            tag.get_text(" ", strip=True)
        )

        if (
            "juventus" in value.lower()
            and len(value) < 180
        ):
            return value

    return "Juventus"


# ============================================================
# HASHTAG
# ============================================================

def hashtag(title: str) -> str:
    title = re.sub(
        r"\b(?:vs|v)\b",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"\bJuventus\b",
        "Juve",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"N\.E\.C\.",
        "NEC",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"[^A-Za-z0-9À-ÖØ-öø-ÿ]",
        "",
        title,
    )

    return title or "Juve"


# ============================================================
# BANDIERE UEFA
# ============================================================

COUNTRY_FLAGS = {
    "ALB": "🇦🇱",
    "AND": "🇦🇩",
    "ARM": "🇦🇲",
    "AUT": "🇦🇹",
    "AZE": "🇦🇿",
    "BEL": "🇧🇪",
    "BIH": "🇧🇦",
    "BUL": "🇧🇬",
    "CRO": "🇭🇷",
    "CYP": "🇨🇾",
    "CZE": "🇨🇿",
    "DEN": "🇩🇰",
    "ENG": "🏴",
    "ESP": "🇪🇸",
    "EST": "🇪🇪",
    "FIN": "🇫🇮",
    "FRA": "🇫🇷",
    "GEO": "🇬🇪",
    "GER": "🇩🇪",
    "GRE": "🇬🇷",
    "HUN": "🇭🇺",
    "IRL": "🇮🇪",
    "ISL": "🇮🇸",
    "ISR": "🇮🇱",
    "ITA": "🇮🇹",
    "KAZ": "🇰🇿",
    "KOS": "🇽🇰",
    "LAT": "🇱🇻",
    "LTU": "🇱🇹",
    "LUX": "🇱🇺",
    "MKD": "🇲🇰",
    "MLT": "🇲🇹",
    "MNE": "🇲🇪",
    "NED": "🇳🇱",
    "NOR": "🇳🇴",
    "POL": "🇵🇱",
    "POR": "🇵🇹",
    "ROU": "🇷🇴",
    "SCO": "🏴",
    "SRB": "🇷🇸",
    "SVK": "🇸🇰",
    "SVN": "🇸🇮",
    "SUI": "🇨🇭",
    "SWE": "🇸🇪",
    "TUR": "🇹🇷",
    "UKR": "🇺🇦",
    "WAL": "🏴",
}


# ============================================================
# FORMATTAZIONE NOMI UEFA
# ============================================================

def format_uefa_name(value: str) -> str:
    value = clean(value)

    # Cerca il codice UEFA della nazionalità alla fine.
    match = re.search(
        r"\b([A-Z]{3})\s*$",
        value,
    )

    if not match:
        return value

    code = match.group(1)

    name = clean(
        value[:match.start()]
    )

    parts = name.split()

    # UEFA restituisce nome + cognome.
    # Nel messaggio vogliamo solo il cognome.
    surname = parts[-1] if parts else name

    flag = COUNTRY_FLAGS.get(
        code,
        "",
    )

    return f"{surname} {flag}".strip()


def format_uefa_assistenti(value: str) -> str:
    value = clean(value)

    # Esempio:
    # Oleksii Myronov UKR Dmytro Zaporozhenko UKR
    #
    # Diventa:
    # Myronov 🇺🇦 – Zaporozhenko 🇺🇦

    matches = re.findall(
        r"(.+?)\s+([A-Z]{3})(?=\s|$)",
        value,
    )

    if not matches:
        return value

    formatted = []

    for name, code in matches:

        name = clean(name)

        parts = name.split()

        surname = (
            parts[-1]
            if parts
            else name
        )

        flag = COUNTRY_FLAGS.get(
            code,
            "",
        )

        formatted.append(
            f"{surname} {flag}".strip()
        )

    return " – ".join(formatted)


# ============================================================
# MESSAGGIO
# ============================================================

def format_message(
    prefix: str,
    title: str,
    roles: dict,
    uefa: bool = False,
) -> str:

    lines = [
        f"{prefix} Designazione arbitrale di #{hashtag(title)}:",
        "",
    ]

    for role in (
        "ARBITRO",
        "ASSISTENTI",
        "IV",
        "VAR",
        "AVAR",
    ):

        value = roles.get(role)

        if not value:
            continue

        if uefa:

            if role == "ASSISTENTI":
                value = format_uefa_assistenti(
                    value
                )

            else:
                value = format_uefa_name(
                    value
                )

        lines.append(
            f"{role}: {value}"
        )

    return "\n".join(lines)


# ============================================================
# CONTROLLO SINGOLA PAGINA
# ============================================================

def process_url(
    url: str,
    prefix: str,
    uefa: bool,
    history: dict,
) -> bool:

    print(f"[ARBITRI] Controllo: {url}")

    try:
        soup = fetch_page(url)

    except Exception as exc:
        print(
            f"[ARBITRI] richiesta fallita: "
            f"{url} -> {exc}"
        )
        return False

    roles = extract_roles(soup)

    if not roles:
        print(
            "[ARBITRI] Nessuna designazione "
            "trovata."
        )
        return False

    title = title_from_soup(soup)

    message = format_message(
        prefix,
        title,
        roles,
        uefa=uefa,
    )

    print(
        "\n"
        + message
        + "\n"
    )

    # La chiave dello storico è l'URL.
    history_key = url

    # Salviamo il messaggio completo.
    previous = history.get(history_key)

    if previous == message:
        print(
            "[ARBITRI] Designazione già "
            "presente nello storico."
        )
        return False

    send_telegram(message)

    history[history_key] = message

    print(
        "[ARBITRI] Nuova designazione "
        "inviata su Telegram."
    )

    return True


# ============================================================
# UEFA
# ============================================================

def check_uefa(history: dict) -> bool:

    # Juventus - NEC
    url = (
        "https://it.uefa.com/"
        "uefaeuropaleague/match/"
        "2050066--juventus-vs-n-e-c/"
        "matchinfo/"
    )

    return process_url(
        url=url,
        prefix="🇪🇺ℹ️",
        uefa=True,
        history=history,
    )


# ============================================================
# SERIE A / COPPA ITALIA / SUPERCOPPA
# ============================================================

def check_italy(history: dict) -> bool:

    # Gli URL italiani vengono letti
    # dal file di configurazione, se presente.

    config_file = DATA_DIR / "partite_italia.json"

    if not config_file.exists():
        print(
            "[ARBITRI] Nessun file "
            "partite_italia.json trovato."
        )
        return False

    try:
        with config_file.open(
            "r",
            encoding="utf-8",
        ) as f:
            matches = json.load(f)

    except Exception as exc:
        print(
            f"[ARBITRI] Errore lettura "
            f"{config_file}: {exc}"
        )
        return False

    if not isinstance(matches, list):
        print(
            "[ARBITRI] partite_italia.json "
            "non contiene una lista."
        )
        return False

    changed = False

    for item in matches:

        if not isinstance(item, dict):
            continue

        url = item.get("url")

        if not url:
            continue

        try:
            result = process_url(
                url=url,
                prefix="🇮🇹ℹ️",
                uefa=False,
                history=history,
            )

            if result:
                changed = True

        except Exception as exc:
            print(
                f"[ARBITRI] Errore durante "
                f"il controllo di {url}: {exc}"
            )

    return changed


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "[ARBITRI] Avvio controllo "
        "designazioni..."
    )

    history = load_history()

    changed = False

    # UEFA
    try:
        if check_uefa(history):
            changed = True

    except Exception as exc:
        print(
            f"[ARBITRI] Errore UEFA: {exc}"
        )

    # Italia
    try:
        if check_italy(history):
            changed = True

    except Exception as exc:
        print(
            f"[ARBITRI] Errore Italia: {exc}"
        )

    if changed:
        save_history(history)
        print(
            "[ARBITRI] Storico aggiornato."
        )

    else:
        print(
            "[ARBITRI] Nessuna nuova "
            "designazione."
        )


if __name__ == "__main__":
    main()
