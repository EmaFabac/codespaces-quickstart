from typing import Any, Dict, List, Text
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet
from rasa_sdk.events import AllSlotsReset
def _norm(s: str) -> str:
    """Normalizira unos: uklanja razmake, mala slova i zamjenjuje čćđšž."""
    s = (s or "").strip().lower()
    return s.replace("č", "c").replace("ć", "c").replace("đ", "d").replace("š", "s").replace("ž", "z")

class ActionSetIsUred(Action):
    def name(self) -> Text: return "action_set_is_ured"
    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        return [SlotSet("is_ured", "ured" in tip)]

class ActionCheckIfSala(Action):
    def name(self) -> Text: return "action_check_if_sala"
    def run(self, dispatcher, tracker, domain):
        # _norm će "Mletačka dvorana" pretvoriti u "mletacka dvorana"
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        is_fiksna = "sala" in tip or "mletacka" in tip
        return [SlotSet("is_sala", is_fiksna)]

class ActionSetSalaOffer(Action):
    def name(self) -> Text: return "action_set_sala_offer"
    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        if "mletacka" in tip:
            cijena, paket = "35 € po satu", "po satu"
        else:
            cijena, paket = "24 €", "dan"
        return [SlotSet("paket", paket), SlotSet("cijena", cijena)]

class ActionRouteOstalo(Action):
    def name(self) -> Text:
        return "action_route_ostalo"

    def run(self, dispatcher, tracker, domain):
        # 1. Hvatanje teksta izravno iz zadnje poruke (ono što je na gumbu)
        user_text = tracker.latest_message.get('text', "")
        
        # 2. Normalizacija za internu provjeru (male, bez kvačica)
        tip_raw = _norm(user_text)
        
        # 3. Logika dodjele - postavljamo TOČAN naziv koji domain očekuje
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
            # Ako je korisnik nešto utipkao, a nije kliknuo gumb
            final_tip = user_text.capitalize()

        # 4. PRISILNO postavljanje slota
        return [SlotSet("tip_usluge", final_tip)]

class ActionSetFlydeskCijena(Action):
    def name(self) -> Text: return "action_set_flydesk_cijena"
    def run(self, dispatcher, tracker, domain):
        tip = _norm(tracker.get_slot("tip_usluge") or "")
        paket_val = tracker.get_slot("paket") or ""
        paket_norm = _norm(paket_val).replace(" ", "_")
        if "konferencij" in tip and "mletacka" not in tip:
            cijena = "105 €" if "sat" in paket_norm else "506 €"
        else:
            cijene = {"1_dan": "13 €", "dan": "13 €", "5_dana": "55 €", "10_dana": "95 €", "1_mjesec": "190 €", "mjesec": "190 €", "3_mjeseca": "540 €"}
            cijena = cijene.get(paket_norm, "13 €")
        return [SlotSet("cijena", cijena)]

class ActionShowUredOffer(Action):
    def name(self) -> Text: return "action_show_ured_offer"
    def run(self, dispatcher, tracker, domain):
        ured = tracker.get_slot("oznaka_ureda")
        dispatcher.utter_message(text=f"Ured {ured} je dostupan za mjesečni najam.")
        return []

class ActionSetPaketMonthly(Action):
    def name(self) -> Text: return "action_set_paket_monthly"
    def run(self, dispatcher, tracker, domain):
        oznaka = tracker.get_slot("oznaka_ureda")
        cijene_ureda = {"04": "304.5 €", "05": "304.5 €", "06": "294 €", "07": "217 €", "09": "400 €"}
        cijena = cijene_ureda.get(oznaka, "350 € + PDV")
        return [SlotSet("paket", "mjesec"), SlotSet("cijena", cijena)]

class ActionCheckIsUredForSummary(Action):
    def name(self) -> Text: return "action_check_is_ured_for_summary"
    def run(self, dispatcher, tracker, domain):
        is_ured = tracker.get_slot("is_ured")
        return [SlotSet("is_ured", is_ured)]

class ActionSaveReservation(Action):
    def name(self) -> Text: return "action_save_reservation"
    def run(self, dispatcher, tracker, domain): return []

class ActionPrepareSummary(Action):
    def name(self) -> Text:
        return "action_prepare_summary"

    def run(self, dispatcher, tracker, domain):
        events = []
        
        # 1. Popravak termina (" i " -> " u ")
        termin = tracker.get_slot("termin") or ""
        if " i " in termin:
            termin = termin.replace(" i ", " u ")
            events.append(SlotSet("termin", termin))
        
        # 2. Provjera praznih polja (da bot ne pukne na kraju)
        slots_to_check = ["napomena", "paket", "termin", "tip_usluge", "ime_prezime", "email"]
        for slot in slots_to_check:
            val = tracker.get_slot(slot)
            if not val: # Ako je None ili prazan string
                events.append(SlotSet(slot, "/"))
        
        return events
    
from rasa_sdk.events import AllSlotsReset

class ActionResetSlots(Action):
    def name(self) -> Text:
        return "action_reset_slots"

    def run(self, dispatcher, tracker, domain):
        # Ova naredba briše apsolutno sve što je bot zapamtio
        # i omogućuje potpuno novi početak
        return [AllSlotsReset()]
    



import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Text, Tuple

from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher

from rapidfuzz import fuzz

_WORD_RE = re.compile(r"[a-zA-ZčćđšžČĆĐŠŽ]+")


def _kb_normalize(text: str) -> str:
    """Lowercase + trim + normalize unicode + collapse spaces."""
    text = (text or "").strip().lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _split_keywords(raw_keywords: Any) -> List[str]:
    """Accepts string 'a, b' or list ['a','b']."""
    if isinstance(raw_keywords, str):
        return [k.strip() for k in raw_keywords.split(",") if k.strip()]
    if isinstance(raw_keywords, list):
        return [str(k).strip() for k in raw_keywords if str(k).strip()]
    return []


def _kb_score(query: str, item: Dict[str, Any]) -> float:
    """
    Score = fuzzy(question) + fuzzy(keywords) + keyword substring boost + word overlap boost.
    RapidFuzz vraća 0..100.
    """
    q = _kb_normalize(query)
    question = _kb_normalize(item.get("question", ""))

    keywords_list = [_kb_normalize(k) for k in _split_keywords(item.get("keywords", ""))]
    keywords_list = [k for k in keywords_list if k]

    # 1) Fuzzy na KB pitanje (najbitnije)
    q_fuzz = fuzz.token_set_ratio(q, question) if question else 0.0

    # 2) Fuzzy na keywords (pomaže za kratke upite)
    kw_fuzz = max((fuzz.partial_ratio(q, kw) for kw in keywords_list), default=0.0)

    # 3) Keyword substring boost (kad se keyword pojavljuje u upitu)
    kw_hit_boost = 0.0
    for kw in keywords_list:
        # ignoriši prekratke riječi (da ne boosta "ida", "je", "su" itd.)
        if len(kw) >= 4 and kw in q:
            kw_hit_boost += 6.0

    # 4) Overlap riječi (mali boost)
    q_words = set(_WORD_RE.findall(q))
    question_words = set(_WORD_RE.findall(question))
    overlap_boost = float(len(q_words.intersection(question_words)) * 2)

    # Težine (fokus na pitanje, keywords samo pomoć)
    return (0.90 * q_fuzz) + (0.20 * kw_fuzz) + kw_hit_boost + overlap_boost


class ActionAnswerFromKB(Action):
    def name(self) -> Text:
        return "action_answer_from_kb"

    def __init__(self) -> None:
        super().__init__()
        self._kb_cache: List[Dict[str, Any]] = []
        self._debug: bool = True  # stavi False kad završiš

        # Pragovi (podešavaj po potrebi)
        self.MIN_SCORE: float = 58.0      # minimalno da uopće odgovori
        self.MIN_MARGIN: float = 6.0      # razlika 1. i 2. mjesta (da ne puca krivo)

    def _resolve_kb_path(self) -> str:
        """
        Očekuje: data/knowledge/IDA_Knowledge_Base.json
        Radi i ako je cwd drugačiji (fallback).
        """
        rel = os.path.join("data", "knowledge", "IDA_Knowledge_Base.json")
        if os.path.exists(rel):
            return rel
        return os.path.join(os.getcwd(), rel)

    def _load_kb(self) -> List[Dict[str, Any]]:
        """Učitavanje JSON datoteke s keširanjem."""
        if self._kb_cache:
            return self._kb_cache

        kb_path = self._resolve_kb_path()

        if self._debug:
            print(f"[KB] Loading from: {kb_path}")

        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                self._kb_cache = json.load(f)

            if self._debug:
                print(f"[KB] Items loaded: {len(self._kb_cache)}")

        except Exception as e:
            print(f"[KB] Greška pri učitavanju JSON-a: {e}")
            return []

        return self._kb_cache

    def _best_match(self, user_msg: str, kb: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
        """Vrati (najbolji_item, best_score) ili (None, 0)."""
        scored: List[Tuple[float, Dict[str, Any]]] = []

        for item in kb:
            try:
                s = float(_kb_score(user_msg, item))
            except Exception:
                s = 0.0
            scored.append((s, item))

        if not scored:
            return None, 0.0

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_item = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if self._debug:
            q = _kb_normalize(user_msg)
            print(f"[KB] Query: {q}")
            print(f"[KB] Best: {best_item.get('id')} score={best_score:.2f} q='{best_item.get('question')}'")
            if len(scored) > 1:
                print(f"[KB] Second: {scored[1][1].get('id')} score={second_score:.2f} q='{scored[1][1].get('question')}'")
            print(f"[KB] Thresholds: MIN_SCORE={self.MIN_SCORE}, MIN_MARGIN={self.MIN_MARGIN}")

        # Filter: minimum score + margin
        if best_score < self.MIN_SCORE:
            return None, best_score
        if (best_score - second_score) < self.MIN_MARGIN:
            return None, best_score

        return best_item, best_score

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:

        user_msg = (tracker.latest_message.get("text", "") or "").strip()

        if not user_msg:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        kb = self._load_kb()
        if not kb:
            dispatcher.utter_message(text="Baza znanja je trenutno nedostupna.")
            return []

        best_item, _ = self._best_match(user_msg, kb)
        if not best_item:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        answer = (best_item.get("answer", "") or "").strip()
        if not answer:
            dispatcher.utter_message(response="utter_kb_fallback")
            return []

        dispatcher.utter_message(text=answer)
        return []
