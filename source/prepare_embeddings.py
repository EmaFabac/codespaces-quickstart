# prepare_embeddings.py
# Pokreni: python3 prepare_embeddings.py
# Ulaz: ida_urls.txt (1 URL po liniji, # za komentar)
# Izlaz: faiss_cache.pkl (format kompatibilan s tvojim actions.py)

import os
import re
import time
import pickle
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer


# ----------------------------
# Postavke
# ----------------------------
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
OUT_PATH = "faiss_cache.pkl"
URLS_PATH = "ida_urls.txt"

CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP = 200

# Malo blaži pragovi da ne izgubiš kratke ključne rečenice (npr. datume)
MIN_PAR_LEN = 20
MIN_CHUNK_LEN = 40


# ----------------------------
# Utils
# ----------------------------
def load_urls(path: str) -> list[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Ne nalazim {path}")

    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    if not urls:
        raise ValueError(f"{path} je prazan (nema URL-ova).")

    return urls


def clean_text(t: str) -> str:
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def path_of(url: str) -> str:
    return urlparse(url).path or "/"


def fetch_page(url: str) -> tuple[str, str, str]:
    """
    Vrati: (full_text_for_embedding, title, h1)
    full_text uključuje TITLE/H1/URL da poboljša pretragu po “nazivima stranica”.
    """
    r = requests.get(url, timeout=25, headers={"User-Agent": "IDA-RasaBot/1.0"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # makni šum
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""

    article = soup.find("article")
    body_text = (article.get_text(separator="\n") if article else soup.get_text(separator="\n"))
    return clean_text(body_text), title, h1_text


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Chunkanje po paragrafima + overlap (da ne reže rečenice).
    Ne filtriramo preagresivno da ne izgubimo kratke ključne rečenice (datumi, definicije).
    """
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    paragraphs = [p for p in paragraphs if len(p) >= MIN_PAR_LEN]

    chunks: list[str] = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) + 1 <= max_chars:
            current = (current + " " + p).strip()
        else:
            if current:
                chunks.append(current.strip())
            tail = current[-overlap:] if current else ""
            current = (tail + " " + p).strip()

    if current:
        chunks.append(current.strip())

    chunks = [c for c in chunks if len(c) >= MIN_CHUNK_LEN]
    return chunks


# ----------------------------
# Main
# ----------------------------
def main():
    urls = load_urls(URLS_PATH)
    model = SentenceTransformer(MODEL_NAME)

    all_chunks: list[str] = []
    meta: list[tuple] = []  # (url, title, h1, path, chunk_i)

    for url in urls:
        try:
            text, title, h1 = fetch_page(url)
            chunks = chunk_text(text)
            header = f"TITLE: {title}\nH1: {h1}\nURL: {url}\nPATH: {path_of(url)}\n\n"

            for i, ch in enumerate(chunks):
                all_chunks.append(header + ch)
                meta.append((url, title, h1, path_of(url), i))

            print(f"OK  {url} -> {len(chunks)} chunks")
            time.sleep(0.4)
        except Exception as e:
            print(f"FAIL {url}: {e}")

    if not all_chunks:
        raise RuntimeError("Nisam izvukao nijedan chunk. Provjeri URL-ove (403/robots) ili parsiranje.")

    emb = model.encode(all_chunks, convert_to_numpy=True, normalize_embeddings=True).astype("float32")
    dim = emb.shape[1]

    index = faiss.IndexFlatIP(dim)  # cosine jer su embeddings normalizirani
    index.add(emb)

    with open(OUT_PATH, "wb") as f:
        pickle.dump(
            {
                "index": index,          # tvoj actions.py očekuje "index"
                "chunks": all_chunks,    # i "chunks"
                "meta": meta,            # i "meta"
                "model": MODEL_NAME,
                "chunk_max_chars": CHUNK_MAX_CHARS,
                "chunk_overlap": CHUNK_OVERLAP,
                "urls_path": URLS_PATH,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
        )

    print(f"Saved {OUT_PATH} with {len(all_chunks)} chunks from {len(urls)} urls")


if __name__ == "__main__":
    main()
