# actions.py
from __future__ import annotations

import os
import re
import csv
import pickle
from datetime import datetime
from typing import Any, Dict, List, Text, Tuple

import numpy as np
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, AllSlotsReset
from sentence_transformers import SentenceTransformer

# --- KB cleanup (makni TITLE/H1/URL/PATH iz chunka) ---
HEADER_LINE_RE = re.compile(r"^\s*(TITLE|H1|URL|PATH)\s*:\s*.*$", re.IGNORECASE)

def clean_kb_text(text: str) -> str:
    """Makni TITLE/H1/URL/PATH blok ako je u chunku."""
    if not text:
        return ""

    lines = [l.rstrip() for l in text.splitlines()]

    cleaned = []
    for l in lines:
        # makni header linije
        if HEADER_LINE_RE.match(l):
            continue
        # makni linije koje su samo "| IDA" ili slične
        if l.strip() in {"| IDA", "|IDA"}:
            continue
        cleaned.append(l)

    out = "\n".join(cleaned).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)  # makni višak praznih linija
    return out

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Normalizira unos: trim, lowercase, zamijeni čćđšž."""
    s = (s or "").strip().lower()
    return (
        s.replace("č", "c")
        .replace("ć", "c")
        .replace("đ", "d")
        .replace("š", "s")
        .replace("ž", "z")
    )


def _has_any(text: str, keywords: List[str]) -> bool:
    t = _norm(text)
    return any(k in t for k in keywords)


DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")


def rerank_faiss_results(
    query: str,
    ids_row: List[int],
    scores_row: List[float],
    meta: List[tuple],
    chunks: List[str],
) -> List[Tuple[float, int, float, str]]:
    """
    Vrati listu (final_score, idx, faiss_score, reason) sortiranu desc.
    final_score = faiss_score + bonus
    meta[idx] očekujemo: (url, title, h1, path, chunk_id)
    """
    q_raw = (query or "").strip()
    q = _norm(q_raw)

    wants_mission = _has_any(q, ["misija", "vizija"])
    wants_about = _has_any(q, ["o nama", "tko smo", "o agenciji", "ida", "ida-e", "idae"])
    wants_founded = _has_any(
        q,
        ["osnovana", "osnovan", "osnutak", "datum osnivanja", "kada je osnovana", "kad je osnovana"],
    )
    wants_contact = _has_any(q, ["kontakt", "kontakti", "telefon", "email", "e-mail", "adresa", "radno vrijeme"])

    # ako je upit ultra kratak i nema "ida", dodaj sidro
    if len(q.split()) <= 2 and not _has_any(q, ["ida", "ida-e", "idae"]):
        q = q + " ida"

    results: List[Tuple[float, int, float, str]] = []

    for rank, idx in enumerate(ids_row):
        if idx == -1:
            continue

        faiss_sc = float(scores_row[rank])

        # meta može biti kraći/duži tuple, ali ti u cache-u imaš 5 elemenata
        m = meta[idx]
        url = m[0] if len(m) > 0 else ""
        title = m[1] if len(m) > 1 else ""
        h1 = m[2] if len(m) > 2 else ""
        path = m[3] if len(m) > 3 else ""
        # chunk_id = m[4] if len(m) > 4 else 0

        title_n = _norm(title)
        h1_n = _norm(h1)
        path_n = _norm(path)
        chunk_txt = chunks[idx] or ""
        chunk_n = _norm(chunk_txt)

        bonus = 0.0
        reasons: List[str] = []

        # Hard preference /o-nama
        if "/o-nama" in path_n or "/o-nama" in _norm(url):
            bonus += 0.08
            reasons.append("about_page")

        # "misija" query -> traži misija u title/h1 i favoriziraj o-nama
        if wants_mission:
            if "misija" in h1_n or "misija" in title_n:
                bonus += 0.16
                reasons.append("mission_in_title_h1")
            if "/o-nama" in path_n:
                bonus += 0.08
                reasons.append("mission_prefers_about")

        # "osnovana/osnutak" query -> preferiraj o-nama + chunk s datumom
        if wants_founded:
            if "/o-nama" in path_n:
                bonus += 0.16
                reasons.append("founded_prefers_about")
            if DATE_RE.search(chunk_txt):
                bonus += 0.14
                reasons.append("has_date")

        # "kontakt" query -> preferiraj kontakti
        if wants_contact:
            if "/kontakti" in path_n or "kontakt" in title_n or "kontakt" in h1_n:
                bonus += 0.18
                reasons.append("contact_page")

        # eksplicitno "o nama"
        if wants_about and ("/o-nama" in path_n):
            bonus += 0.10
            reasons.append("explicit_about")

        # penaliziraj novosti kod generičkih upita (da ne dominiraju)
        if "/novosti" in path_n or "novosti" in title_n:
            bonus -= 0.06
            reasons.append("news_penalty")

        # mali bonus ako query riječi postoje doslovno u chunku (lexical hint)
        # (ovo često spasi "osnovana 1999" jer se pojavi u chunku)
        if wants_founded and _has_any(chunk_n, ["osnovan", "osnovana", "osnut"]):
            bonus += 0.06
            reasons.append("lex_founded")

        final = faiss_sc + bonus
        results.append((final, idx, faiss_sc, ",".join(reasons)))

    results.sort(key=lambda x: x[0], reverse=True)
    return results


# -----------------------------------------------------------------------------
# 1) FILTER SLOT FILLING (najbitnije za tvoj bug)
# -----------------------------------------------------------------------------
RESERVATION_ONLY_SLOTS = {
    "termin_input",
    "ime_prezime_input",
    "napomena_input",
    "email",
    "naziv_pravne_osobe",
    "ovlastena_osoba",
    "adresa",
    "telefon",
    "web_stranica",
    "oib",
    "sektor",
    "broj_zaposlenih",
    "datum_osnivanja",
    "opis_poslovanja",
    "inovativni_aspekti",
    "doprinos_zajednici",
    "ostale_napomene",
    "mjesto_datum",
    "paket",
    "oznaka_ureda",
    "termin",
}


class ActionExtractSlots(Action):
    """
    Rasa interno zove action_extract_slots i pokušava popuniti slotove.
    Ova akcija briše slučajno popunjene rezervacijske slotove kad nismo u rezervaciji.
    """

    def name(self) -> Text:
        return "action_extract_slots"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        in_res = tracker.get_slot("in_reservation") is True
        if in_res:
            return []

        events: List[Dict[Text, Any]] = []
        for slot in RESERVATION_ONLY_SLOTS:
            if tracker.get_slot(slot) is not None:
                events.append(SlotSet(slot, None))
        return events


# -----------------------------------------------------------------------------
# 2) REZERVACIJE - tvoje postojeće akcije
# -----------------------------------------------------------------------------
class ActionSetIsUred(Action):
    def name(self) -> Text:
        return "action_set_is_ured"

    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        return [SlotSet("is_ured", "ured" in tip)]


class ActionCheckIfSala(Action):
    def name(self) -> Text:
        return "action_check_if_sala"

    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        is_fiksna = "sala" in tip or "mletacka" in tip
        return [SlotSet("is_sala", is_fiksna)]


class ActionSetSalaOffer(Action):
    def name(self) -> Text:
        return "action_set_sala_offer"

    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        if "mletacka" in tip:
            cijena, paket = "35 € po satu", "1 sat"
        else:
            cijena, paket = "24 €", "dan"
        return [SlotSet("paket", paket), SlotSet("cijena", cijena)]


class ActionRouteOstalo(Action):
    def name(self) -> Text:
        return "action_route_ostalo"

    def run(self, dispatcher, tracker, domain):
        user_text = tracker.latest_message.get("text", "")
        tip_raw = _norm(user_text)

        if "mletacka" in tip_raw:
            final_tip = "Mletačka dvorana"
        elif "konferencij" in tip_raw:
            final_tip = "Konferencijska dvorana"
        elif "flydesk" in tip_raw:
            final_tip = "Flydesk"
        elif "sala" in tip_raw or "sastank" in tip_raw:
            final_tip = "Sala za sastanke"
        elif "ured" in tip_raw:
            final_tip = "Uredski prostor"
        else:
            final_tip = user_text.capitalize()

        return [SlotSet("tip_usluge", final_tip)]


class ActionSetFlydeskCijena(Action):
    def name(self) -> Text:
        return "action_set_flydesk_cijena"

    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        paket_val = tracker.get_slot("paket") or ""
        paket_norm = _norm(paket_val).replace(" ", "_")

        if "konferencij" in tip and "mletacka" not in tip:
            cijena = "105 €" if "sat" in paket_norm else "506 €"
        else:
            cijene = {
                "1_dan": "13 €",
                "dan": "13 €",
                "5_dana": "55 €",
                "10_dana": "95 €",
                "1_mjesec": "190 €",
                "mjesec": "190 €",
                "3_mjeseca": "540 €",
            }
            cijena = cijene.get(paket_norm, "13 €")

        return [SlotSet("cijena", cijena)]


class ActionShowUredOffer(Action):
    def name(self) -> Text:
        return "action_show_ured_offer"

    def run(self, dispatcher, tracker, domain):
        ured = tracker.get_slot("oznaka_ureda")
        dispatcher.utter_message(text=f"Ured {ured} je dostupan za mjesečni najam.")
        return []


class ActionSetPaketMonthly(Action):
    def name(self) -> Text:
        return "action_set_paket_monthly"

    def run(self, dispatcher, tracker, domain):
        oznaka = tracker.get_slot("oznaka_ureda")
        cijene_ureda = {"04": "304.5 €", "05": "304.5 €", "06": "294 €", "07": "217 €", "09": "400 €"}
        cijena = cijene_ureda.get(oznaka, "350 € + PDV")
        return [SlotSet("paket", "mjesec"), SlotSet("cijena", cijena)]


class ActionCheckIsUredForSummary(Action):
    def name(self) -> Text:
        return "action_check_is_ured_for_summary"

    def run(self, dispatcher, tracker, domain):
        is_ured = tracker.get_slot("is_ured")
        return [SlotSet("is_ured", is_ured)]


class ActionSaveReservation(Action):
    def name(self) -> Text:
        return "action_save_reservation"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        path = "data/reservations.csv"
        os.makedirs(os.path.dirname(path), exist_ok=True)

        row = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "tip_usluge": tracker.get_slot("tip_usluge") or "",
            "oznaka_ureda": tracker.get_slot("oznaka_ureda") or "",
            "paket": tracker.get_slot("paket") or "",
            "cijena": tracker.get_slot("cijena") or "",
            "termin": tracker.get_slot("termin") or "",
            "ime_prezime": tracker.get_slot("ime_prezime") or "",
            "email": tracker.get_slot("email") or "",
            "napomena": tracker.get_slot("napomena") or "",
            "naziv_pravne_osobe": tracker.get_slot("naziv_pravne_osobe") or "",
            "ovlastena_osoba": tracker.get_slot("ovlastena_osoba") or "",
            "adresa": tracker.get_slot("adresa") or "",
            "telefon": tracker.get_slot("telefon") or "",
            "web_stranica": tracker.get_slot("web_stranica") or "",
            "oib": tracker.get_slot("oib") or "",
            "sektor": tracker.get_slot("sektor") or "",
            "broj_zaposlenih": tracker.get_slot("broj_zaposlenih") or "",
            "datum_osnivanja": tracker.get_slot("datum_osnivanja") or "",
            "opis_poslovanja": tracker.get_slot("opis_poslovanja") or "",
            "inovativni_aspekti": tracker.get_slot("inovativni_aspekti") or "",
            "doprinos_zajednici": tracker.get_slot("doprinos_zajednici") or "",
            "ostale_napomene": tracker.get_slot("ostale_napomene") or "",
            "mjesto_datum": tracker.get_slot("mjesto_datum") or "",
            "conversation_id": tracker.sender_id,
        }

        fields = list(row.keys())
        file_exists = os.path.exists(path)

        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if not file_exists:
                w.writeheader()
            w.writerow(row)

        return []


class ActionPrepareSummary(Action):
    def name(self) -> Text:
        return "action_prepare_summary"

    def run(self, dispatcher, tracker, domain):
        events: List[Dict[Text, Any]] = []

        termin = tracker.get_slot("termin") or ""
        if " i " in termin:
            termin = termin.replace(" i ", " u ")
            events.append(SlotSet("termin", termin))

        slots_to_check = ["napomena", "paket", "termin", "tip_usluge", "ime_prezime", "email"]
        for slot in slots_to_check:
            val = tracker.get_slot(slot)
            if not val:
                events.append(SlotSet(slot, "/"))

        return events


class ActionResetSlots(Action):
    def name(self) -> Text:
        return "action_reset_slots"

    def run(self, dispatcher, tracker, domain):
        return [AllSlotsReset()]


# -----------------------------------------------------------------------------
# 3) VALIDACIJE / SETTERI
# -----------------------------------------------------------------------------
class ActionSetImePrezime(Action):
    def name(self) -> Text:
        return "action_set_ime_prezime"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        text = (tracker.get_slot("ime_prezime_input") or "").strip()

        if len(text.split()) < 2:
            dispatcher.utter_message(text="Molim upišite ime i prezime (npr. Lovro Turnić).")
            return [SlotSet("ime_prezime_input", None)]

        return [SlotSet("ime_prezime", text), SlotSet("ime_prezime_input", None)]


class ActionSetNapomena(Action):
    def name(self) -> Text:
        return "action_set_napomena"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        text = (tracker.get_slot("napomena_input") or "").strip()

        if not text:
            dispatcher.utter_message(text="Upišite napomenu (ili napišite 'Bez napomene').")
            return [SlotSet("napomena_input", None)]

        return [SlotSet("napomena", text), SlotSet("napomena_input", None)]


TERM_SAT_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.?\s*u\s*\d{1,2}(:\d{2})?\s*$")


class ActionSetTermin(Action):
    def name(self) -> Text:
        return "action_set_termin"

    def run(self, dispatcher, tracker, domain):
        raw = (tracker.get_slot("termin_input") or "").strip()

        if not TERM_SAT_RE.match(raw):
            dispatcher.utter_message(text="Molim unesite u formatu: 2.2. u 11 ili 2.2. u 11:30.")
            return [SlotSet("termin_input", None)]

        return [SlotSet("termin", raw), SlotSet("termin_input", None)]


class ActionRouteTermin(Action):
    def name(self) -> Text:
        return "action_route_termin"

    def run(self, dispatcher, tracker, domain):
        return []


class ActionRouteUredExtra(Action):
    def name(self) -> Text:
        return "action_route_ured_extra"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        return []


class ActionSetInReservationTrue(Action):
    def name(self) -> Text:
        return "action_set_in_reservation_true"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        return [SlotSet("in_reservation", True)]


class ActionSetInReservationFalse(Action):
    def name(self) -> Text:
        return "action_set_in_reservation_false"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        return [SlotSet("in_reservation", False)]


# -----------------------------------------------------------------------------
# 4) WEB / KB (FAISS cache)
# -----------------------------------------------------------------------------
CACHE_PATH = os.path.join(os.path.dirname(__file__), "faiss_cache.pkl")

TOP_K = 10

# stari pragovi (faiss-only) ti više nisu ključni; zadržavamo za fallback logiku
MIN_SCORE = 0.35
MIN_MARGIN = 0.02

# novi prag za rerank
MIN_FINAL_SCORE = 0.35

MAX_CHARS = 900

DEBUG_KB = os.getenv("KB_DEBUG") == "1"  # export KB_DEBUG=1 za debug

_store = None
_model = None


def load_store():
    global _store, _model
    if _store is None:
        if not os.path.exists(CACHE_PATH):
            raise FileNotFoundError(f"Ne nalazim faiss cache: {CACHE_PATH}")
        with open(CACHE_PATH, "rb") as f:
            _store = pickle.load(f)
        _model = SentenceTransformer(_store["model"])
    return _store, _model


def _faiss_best_id(scores, ids) -> int | None:
    """
    Legacy: ostavljeno radi kompatibilnosti, ali više ne koristimo kao glavni odabir.
    """
    if ids is None or len(ids) == 0 or len(ids[0]) == 0:
        return None
    if ids[0][0] == -1:
        return None

    best = float(scores[0][0])
    second = float(scores[0][1]) if len(ids[0]) > 1 and ids[0][1] != -1 else -1.0

    if best < MIN_SCORE:
        return None

    if second > -1.0 and (best - second) < MIN_MARGIN:
        return None

    return int(ids[0][0])


def _pick_best_with_rerank(
    user_q: str,
    scores,
    ids,
    meta,
    chunks,
    dispatcher: CollectingDispatcher | None = None,
) -> Tuple[int | None, float]:
    """
    Vrati (best_idx, best_final_score). best_idx=None ako nema dovoljno dobar rezultat.
    """
    if ids is None or len(ids) == 0 or len(ids[0]) == 0:
        return None, 0.0

    ids_row = [int(x) for x in ids[0] if int(x) != -1]
    scores_row = [float(scores[0][i]) for i in range(len(ids[0])) if int(ids[0][i]) != -1]

    if not ids_row:
        return None, 0.0

    reranked = rerank_faiss_results(user_q, ids_row, scores_row, meta, chunks)

    if DEBUG_KB and dispatcher is not None:
        debug_lines = []
        for r, (final_sc, idx, faiss_sc, reason) in enumerate(reranked[:10], start=1):
            url = meta[idx][0]
            snippet = (chunks[idx] or "")[:170].replace("\n", " ")
            debug_lines.append(f"{r}) final={final_sc:.3f} faiss={faiss_sc:.3f} | {url} | {reason} | {snippet}")
        print("DEBUG RERANK:\n" + "\n".join(debug_lines))

    best_final, best_idx, best_faiss, best_reason = reranked[0]

    # prag
    if best_final < MIN_FINAL_SCORE:
        return None, best_final

    # opcionalno: ako je preblizu drugi rezultat, možeš fallbackati
    if len(reranked) > 1:
        second_final = reranked[1][0]
        if (best_final - second_final) < MIN_MARGIN:
            return None, best_final

    return int(best_idx), float(best_final)


class ActionAnswerFromKB(Action):
    def name(self) -> str:
        return "action_answer_from_kb"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: dict):
        user_q = (tracker.latest_message.get("text") or "").strip()
        if not user_q:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        try:
            store, model = load_store()
        except Exception:
            dispatcher.utter_message(text="Trenutno ne mogu pristupiti bazi znanja. Pokušajte kasnije.")
            return []

        index = store["index"]
        chunks = store["chunks"]
        meta = store["meta"]

        q_emb = model.encode([user_q], convert_to_numpy=True, normalize_embeddings=True)
        q_emb = np.asarray(q_emb, dtype="float32")

        scores, ids = index.search(q_emb, TOP_K)

        # rerank izbor (umjesto top1)
        best_idx, best_final = _pick_best_with_rerank(user_q, scores, ids, meta, chunks, dispatcher=dispatcher)
        if best_idx is None:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        url = meta[best_idx][0]
        text = clean_kb_text((chunks[best_idx] or "").strip())

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rsplit(" ", 1)[0] + "..."

        dispatcher.utter_message(text=f"{text}\n\nIzvor: {url}")
        return []


# -----------------------------------------------------------------------------
# 5) FAQ JSON + router (FAQ -> KB fallback)
# -----------------------------------------------------------------------------
import json
from pathlib import Path

FAQ_PATH = Path("/workspaces/codespaces-quickstart/source/data/knowledge/IDA_Knowledge_Base.json")
FAQ_CACHE_PATH = Path(__file__).resolve().parent / "faq_cache.pkl"

FAQ_TOP_K = 3
FAQ_MIN_SCORE = 0.55
FAQ_MAX_CHARS = 1200

_faq_store = None


def load_faq_store(model: SentenceTransformer):
    """
    Učita FAQ json i napravi embeddings cache (pickle).
    Cache se učita ako postoji, inače se generira.
    """
    global _faq_store

    if _faq_store is not None:
        return _faq_store

    if os.path.exists(FAQ_CACHE_PATH):
        with open(FAQ_CACHE_PATH, "rb") as f:
            _faq_store = pickle.load(f)
        return _faq_store

    if not os.path.exists(FAQ_PATH):
        raise FileNotFoundError(f"Ne nalazim FAQ json: {FAQ_PATH}")

    with open(FAQ_PATH, "r", encoding="utf-8") as f:
        faq = json.load(f)

    texts = []
    items = []
    for item in faq:
        q = item.get("question", "")
        kw = item.get("keywords", "")
        cat = item.get("category", "")

        search_text = f"{q}\n{kw}\n{cat}".strip()
        texts.append(search_text)
        items.append(item)

    emb = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    emb = np.asarray(emb, dtype="float32")

    _faq_store = {"items": items, "emb": emb}

    with open(FAQ_CACHE_PATH, "wb") as f:
        pickle.dump(_faq_store, f)

    return _faq_store


def search_faq(user_q: str, model: SentenceTransformer):
    """
    Vrati najbolji FAQ item i score (cosine sim jer su embedding normalizirani).
    """
    store = load_faq_store(model)
    emb = store["emb"]
    items = store["items"]

    q_emb = model.encode([user_q], convert_to_numpy=True, normalize_embeddings=True)
    q_emb = np.asarray(q_emb, dtype="float32")[0]

    scores = emb @ q_emb
    best_idx = int(np.argmax(scores))
    best_score = float(scores[best_idx])

    return items[best_idx], best_score


class ActionAnswer(Action):
    """
    Router:
    1) FAQ (json) -> ako score dovoljno dobar
    2) inače KB (FAISS) fallback, ali s rerank (title/h1/path boost)
    """

    def name(self) -> Text:
        return "action_answer"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]):
        user_q = (tracker.latest_message.get("text") or "").strip()
        if not user_q:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        # učitaj KB store+model
        try:
            kb_store, model = load_store()
        except Exception:
            dispatcher.utter_message(text="Trenutno ne mogu pristupiti bazi znanja. Pokušajte kasnije.")
            return []

        # 1) FAQ
        try:
            item, score = search_faq(user_q, model)
        except Exception:
            item, score = None, 0.0

        if item is not None and score >= FAQ_MIN_SCORE:
            answer = (item.get("answer") or "").strip()
            source = (item.get("source") or "").strip()

            if len(answer) > FAQ_MAX_CHARS:
                answer = answer[:FAQ_MAX_CHARS].rsplit(" ", 1)[0] + "..."

            dispatcher.utter_message(text=f"{answer}\n\nIzvor: {source}" if source else answer)
            return []

        # 2) KB fallback (FAISS + rerank)
        index = kb_store["index"]
        chunks = kb_store["chunks"]
        meta = kb_store["meta"]

        q_emb = model.encode([user_q], convert_to_numpy=True, normalize_embeddings=True)
        q_emb = np.asarray(q_emb, dtype="float32")

        scores, ids = index.search(q_emb, TOP_K)

        best_idx, best_final = _pick_best_with_rerank(user_q, scores, ids, meta, chunks, dispatcher=dispatcher)
        if best_idx is None:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        url = meta[best_idx][0]
        text = clean_kb_text((chunks[best_idx] or "").strip())

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rsplit(" ", 1)[0] + "..."

        dispatcher.utter_message(text=f"{text}\n\nIzvor: {url}")
        return []
