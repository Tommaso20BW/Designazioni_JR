import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = DATA_DIR / "designazioni.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID."
    )


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
        print(
            f"[STORICO] Impossibile leggere lo storico: {exc}"
        )

    return {}


def save_history(history: dict) -> None:
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            history,
            f,
            ensure_ascii=False,
            indent=2,
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

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

def fetch_page_requests(url: str) -> BeautifulSoup:
    print("[ITALIA] Utilizzo requests...")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=45,
    )

    response.raise_for_status()

    return BeautifulSoup(
        response.text,
        "html.parser",
    )


def fetch_page_playwright(url: str) -> BeautifulSoup:
    print("[UEFA] Avvio Playwright...")

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"]
        )

        try:
            print(f"[UEFA] Apertura pagina: {url}")

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            # La pagina UEFA è dinamica.
            # Aspettiamo il caricamento della rete.
            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=15000,
                )
            except Exception:
                pass

            # Tempo aggiuntivo per il rendering
            # dei dati caricati dinamicamente.
            page.wait_for_timeout(3000)

            html = page.content()

        finally:
            browser.close()

    return BeautifulSoup(
        html,
        "html.parser",
    )


# ============================================================
# PARSER ITALIA
# ============================================================

def extract_roles_italy(soup: BeautifulSoup) -> dict:
    result: dict[str, str] = {}

    # Prima cerchiamo nei singoli elementi.
    for tag in soup.find_all(
        [
            "li",
            "p",
            "div",
            "span",
            "td",
            "tr",
        ]
    ):

        value = clean(
            tag.get_text(
                " ",
                strip=True,
            )
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
                    extracted = clean(
                        match.group(1)
                    )

                    if extracted:
                        result[role] = extracted

                    break

    # Fallback sul testo completo.
    if not result:

        text = clean(
            soup.get_text(
                " ",
                strip=True,
            )
        )

        boundary = "|".join(
            re.escape(alias)
            for aliases in ROLES.values()
            for alias in aliases
        )

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
                    extracted = clean(
                        match.group(1)
                    ).strip(":- ")

                    if extracted:
                        result[role] = extracted
                        break

    return result


# ============================================================
# PARSER UEFA
# ============================================================

def clean_uefa_value(value: str) -> str:
    """
    Restituisce solamente il blocco:
    Nome Cognome COD
    ignorando completamente il testo successivo.
    """

    value = clean(value)

    if not value:
        return ""

    # Primo codice UEFA trovato.
    match = re.search(
        r"\b([A-Z]{3})\b",
        value,
    )

    if match:
        value = value[:match.end()]

    return clean(value).strip(":- ")


def extract_uefa_role_from_element(
    element,
    aliases: list[str],
) -> str:

    text = clean(
        element.get_text(
            " ",
            strip=True,
        )
    )

    if not text or len(text) > 500:
        return ""

    for alias in aliases:

        match = re.match(
            rf"^{re.escape(alias)}\s*[:\-]?\s*(.+)$",
            text,
            re.I,
        )

        if match:
            return clean_uefa_value(
                match.group(1)
            )

    return ""


def extract_uefa_roles(soup: BeautifulSoup) -> dict:
    """
    Parser UEFA.

    Non utilizza più il body completo come prima scelta.
    Cerca i ruoli nei singoli elementi HTML e limita
    ogni valore al primo codice nazionale UEFA.
    """

    result: dict[str, str] = {}

    tags = soup.find_all(
        [
            "li",
            "p",
            "div",
            "span",
            "td",
            "tr",
        ]
    )

    # Gli elementi più piccoli sono generalmente
    # quelli che contengono il singolo ruolo.
    tags = sorted(
        tags,
        key=lambda tag: len(
            clean(
                tag.get_text(
                    " ",
                    strip=True,
                )
            )
        )
    )

    for tag in tags:

        text = clean(
            tag.get_text(
                " ",
                strip=True,
            )
        )

        if not text or len(text) > 500:
            continue

        for role, aliases in ROLES.items():

            if role in result:
                continue

            value = extract_uefa_role_from_element(
                tag,
                aliases,
            )

            if value:
                result[role] = value

                print(
                    f"[UEFA] {role}: {value}"
                )

                break

    # --------------------------------------------------------
    # Fallback molto restrittivo.
    # --------------------------------------------------------

    if not result:

        print(
            "[UEFA] Nessun ruolo trovato "
            "nei singoli elementi."
        )

        text = clean(
            soup.get_text(
                " ",
                strip=True,
            )
        )

        for role, aliases in ROLES.items():

            for alias in aliases:

                # IMPORTANTISSIMO:
                # il match termina al PRIMO codice
                # nazionale di tre lettere.
                pattern = (
                    rf"\b{re.escape(alias)}\b"
                    rf"\s*[:\-]?\s*"
                    rf"(.+?\b[A-Z]{{3}}\b)"
                )

                match = re.search(
                    pattern,
                    text,
                    re.I,
                )

                if not match:
                    continue

                value = clean_uefa_value(
                    match.group(1)
                )

                if value:
                    result[role] = value

                    print(
                        f"[UEFA] {role} fallback: "
                        f"{value}"
                    )

                    break

    return result


def extract_roles(
    soup: BeautifulSoup,
    uefa: bool = False,
) -> dict:

    if uefa:
        return extract_uefa_roles(soup)

    return extract_roles_italy(soup)


# ============================================================
# TITOLO UEFA
# ============================================================

def uefa_match_title(url: str) -> str:
    """
    Per UEFA non utilizziamo il <title> della pagina.

    L'URL della partita contiene:
    juventus-vs-n-e-c

    e viene trasformato direttamente in:
    Juventus - N.E.C.
    """

    match = re.search(
        r"/match/\d+--([^/]+)/",
        url,
        re.I,
    )

    if not match:
        return "Juventus"

    slug = match.group(1)

    teams = re.split(
        r"-(?:vs|v)-",
        slug,
        flags=re.I,
    )

    if len(teams) != 2:
        return "Juventus"

    def format_team(value: str) -> str:

        value = value.replace(
            "-",
            " ",
        )

        if value.lower() == "juventus":
            return "Juventus"

        if value.lower() in {
            "n e c",
            "n.e.c.",
            "nec",
        }:
            return "N.E.C."

        return value.title()

    home = format_team(
        teams[0]
    )

    away = format_team(
        teams[1]
    )

    return f"{home} - {away}"


def title_from_soup(
    soup: BeautifulSoup,
    url: str = "",
    uefa: bool = False,
) -> str:

    if uefa and url:
        return uefa_match_title(url)

    # Italia: prova gli heading.
    for tag in soup.find_all(
        [
            "h1",
            "h2",
        ]
    ):

        value = clean(
            tag.get_text(
                " ",
                strip=True,
            )
        )

        if (
            "juventus" in value.lower()
            and len(value) < 180
        ):
            return value

    # Poi il title HTML.
    if soup.title:

        value = clean(
            soup.title.get_text(
                " ",
                strip=True,
            )
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

    value = clean_uefa_value(value)

    match = re.search(
        r"\b([A-Z]{3})\b",
        value,
    )

    if not match:
        return value

    code = match.group(1)

    name = clean(
        value[:match.start()]
    )

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

    return f"{surname} {flag}".strip()


def format_uefa_assistenti(value: str) -> str:
    """
    Gestisce:
    Nome Cognome UKR Nome Cognome UKR

    restituendo:
    Cognome 🇺🇦 – Cognome 🇺🇦
    """

    value = clean(value)

    # Cerchiamo blocchi terminanti con un codice paese.
    matches = re.findall(
        r"([A-Za-zÀ-ÖØ-öø-ÿ'’-]+"
        r"(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ'’-]+)*)"
        r"\s+([A-Z]{3})\b",
        value,
    )

    if not matches:
        return value

    formatted = []

    for name, code in matches:

        name = clean(name)

        parts = name.split()

        if not parts:
            continue

        surname = parts[-1]

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

    print(
        f"[ARBITRI] Controllo: {url}"
    )

    try:

        if uefa:
            soup = fetch_page_playwright(
                url
            )

        else:
            soup = fetch_page_requests(
                url
            )

    except Exception as exc:

        print(
            f"[ARBITRI] richiesta fallita: "
            f"{url} -> {exc}"
        )

        return False

    roles = extract_roles(
        soup,
        uefa=uefa,
    )

    if not roles:

        print(
            "[ARBITRI] Nessuna designazione "
            "trovata."
        )

        return False

    title = title_from_soup(
        soup,
        url=url,
        uefa=uefa,
    )

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

    history_key = url

    previous = history.get(
        history_key
    )

    # Se il messaggio è identico non inviamo
    # nuovamente la designazione.
    if previous == message:

        print(
            "[ARBITRI] Designazione già "
            "presente nello storico."
        )

        return False

    # Se il vecchio storico è sporco/sbagliato,
    # viene automaticamente sostituito con quello corretto.
    if previous:
        print(
            "[STORICO] Designazione precedente "
            "diversa: aggiorno il record."
        )

    send_telegram(
        message
    )

    history[history_key] = message

    print(
        "[ARBITRI] Nuova designazione "
        "inviata su Telegram."
    )

    return True


# ============================================================
# UEFA
# ============================================================

def check_uefa(
    history: dict,
) -> bool:

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

def check_italy(
    history: dict,
) -> bool:

    config_file = (
        DATA_DIR /
        "partite_italia.json"
    )

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

    if not isinstance(
        matches,
        list,
    ):

        print(
            "[ARBITRI] partite_italia.json "
            "non contiene una lista."
        )

        return False

    changed = False

    for item in matches:

        if not isinstance(
            item,
            dict,
        ):
            continue

        url = item.get(
            "url"
        )

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

    # UEFA → Playwright
    try:

        if check_uefa(
            history
        ):
            changed = True

    except Exception as exc:

        print(
            f"[ARBITRI] Errore UEFA: {exc}"
        )

    # Italia → requests
    try:

        if check_italy(
            history
        ):
            changed = True

    except Exception as exc:

        print(
            f"[ARBITRI] Errore Italia: {exc}"
        )

    if changed:

        save_history(
            history
        )

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
    
