    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", value, re.I)
    if m and m.group(2).lower() in months:
        return f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def article_links(soup):
    """Estrae le sole pagine di designazione Serie A ENILIVE."""
    result = []
    seen = set()

    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" ", strip=True)).replace("\ufeff", "")
        href = urljoin(AIA, a["href"]).split("#", 1)[0]
        lower = href.lower()

        if "/news/" not in lower:
            continue
        if "serie-a-enilive" not in lower:
            continue
        if "designazioni" not in lower:
            continue
        if "variazione" in lower or "variazione" in label.lower():
            continue
        if label and not _looks_like_italia_designation_title(label):
            continue

        if href not in seen:
            result.append(href)
            seen.add(href)

    return result


def _italia_state_has_url(state, url):
    """Compatibile con vecchi e nuovi formati del file di stato."""
    bucket = state.setdefault("italia", {})
    if url in bucket:
        return True
    suffix = ":" + url
    return any(key.endswith(suffix) for key in bucket)


def _article_id(url):
    """ID numerico finale dell'articolo AIA, usato per scegliere il più recente."""
    m = re.search(r"-(\d+)/?$", url)
    return int(m.group(1)) if m else -1


def check_italia(state):
    """Controlla l'ultima designazione Serie A AIA con log compatto, come UEFA."""
    articoli = 0
    gia_inviate = 0
    nuove = 0

    try:
        response = request(AIA)
        index = BeautifulSoup(response.content, "html.parser")
    except Exception:
        print("[ITALIA] Serie A: errore nel calendario")
        return False

    links = article_links(index)
    if not links:
        print("[ITALIA] Serie A: 0 articoli, 0 già inviati, 0 nuove")
        return False

    articoli = 1
    ultimo = max(links, key=_article_id)

    if _italia_state_has_url(state, ultimo):
        gia_inviate = 1
        print(f"[ITALIA] Serie A: 1 articolo, {gia_inviate} già inviato, 0 nuove")
        return False

    try:
        response = request(ultimo)
        soup = BeautifulSoup(response.content, "html.parser")
    except Exception:
        print("[ITALIA] Serie A: errore nella designazione")
        print(f"[ITALIA] Serie A: {articoli} articolo, 0 già inviati, 0 nuove")
        return False

    title, roles = extract_italia_assignment(soup)
    if not title or not roles.get("ARBITRO"):
        print(f"[ITALIA] Serie A: {articoli} articolo, 0 già inviati, 0 nuove")
        return False

    message, entities = format_message_italia(title, roles)

    try:
        send_telegram(message, entities)
    except Exception:
        print("[ITALIA] Serie A: invio Telegram fallito")
        print(f"[ITALIA] Serie A: {articoli} articolo, 0 già inviati, 0 nuove")
        return False

    state["italia"][ultimo] = {
        "date": datetime.now(ROME).date().isoformat(),
        "match": title,
        "url": ultimo,
        "sent_at": datetime.now(ROME).isoformat(),
    }
    save_state(state)
    nuove = 1

    print(f"[ITALIA] ✅ inviata: {title}")
    print(f"[ITALIA] Serie A: {articoli} articolo, 0 già inviati, {nuove} nuova")
    return True


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
