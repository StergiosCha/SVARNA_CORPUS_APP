"""
download_literature.py — Script to download, classify, and organize Greek literature datasets.

Downloads:
1. Project Gutenberg Greek books (filtered, classified into Ancient/Katharevousa/Modern + Genre)
2. BabyLM-ell (Hugging Face Greek e-books & literature subset)
3. Interwar Greek Poetry (StergiosCha/RAG-poetry repository)

Saves files in compressed formats to optimize disk space.
"""

import os
import re
import sys
import json
import gzip
import shutil
import zipfile
import requests
from pathlib import Path

# Set up paths
LIT_DIR = Path(__file__).resolve().parent
GUTENBERG_DIR = LIT_DIR / "gutenberg"
BABYLM_DIR = LIT_DIR / "babylm"
POETRY_DIR = LIT_DIR / "poetry"

for d in (GUTENBERG_DIR, BABYLM_DIR, POETRY_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Classification Helpers
# ---------------------------------------------------------------------------

def strip_diacritics(text: str) -> str:
    """Normalize Greek text to base characters by stripping diacritics."""
    import unicodedata
    nfd = unicodedata.normalize('NFD', text)
    return ''.join(c for c in nfd if not unicodedata.combining(c))


def heal_text_mojibake(text: str) -> str:
    """Detect if a text is actually Windows-1253 encoded but loaded as ISO-8859-1, and heal it."""
    # Count typical mojibake characters vs Greek characters in the first 10k chars
    mojibake_chars = len(re.findall(r'[ÔßôëïòÁÅÇÉÊÑÓÕ×ÞÝáãäåæçèéêëìíîïðñóôõö÷øùúûüýþ]', text[:10000]))
    greek_chars = len(re.findall(r'[α-ωΑ-Ωἀ-Ὧ]', text[:10000]))
    
    if mojibake_chars > 100 and greek_chars < 50:
        # Build mapping from windows-1252 characters to windows-1253
        w1252_to_w1253 = {}
        for b in range(128, 256):
            try:
                c1252 = bytes([b]).decode('windows-1252')
                c1253 = bytes([b]).decode('windows-1253')
                w1252_to_w1253[c1252] = c1253
            except Exception:
                pass
        return ''.join(w1252_to_w1253.get(c, c) for c in text)
    return text


def classify_greek_variety(text: str, author_names: list[str]) -> str:
    """Classify text into Ancient Greek, Katharevousa, or Modern Greek."""
    # Normalize text to unaccented lowercase Greek characters
    text_lower = text.lower()
    normalized = strip_diacritics(text_lower)
    words = re.findall(r'\b[α-ω]+\b', normalized)
    
    # Define variety-specific lexical markers
    ancient_words = ['μεν', 'δε', 'γαρ', 'ουν', 'ουκ', 'ουχ', 'εστι', 'εισι', 'ην']
    katharevousa_words = ['ητο', 'ησαν', 'ειχον', 'μονον', 'εις', 'οστις', 'τω']
    modern_words = ['ηταν', 'ειχαν', 'ειχε', 'για', 'που', 'μονο', 'σε', 'θα']
    
    anc_cnt = sum(words.count(w) for w in ancient_words)
    kath_cnt = sum(words.count(w) for w in katharevousa_words)
    mod_cnt = sum(words.count(w) for w in modern_words)
    
    poly_cnt = len(re.findall(r'[\u1f00-\u1ffe]', text))
    poly_ratio = poly_cnt / max(len(text), 1)
    
    total_hits = anc_cnt + kath_cnt + mod_cnt
    if total_hits < 10:
        if poly_ratio > 0.01:
            return 'Ancient Greek' if anc_cnt >= kath_cnt else 'Katharevousa'
        else:
            return 'Modern Greek'
    elif anc_cnt > mod_cnt * 1.5 and anc_cnt > kath_cnt * 1.1:
        return 'Ancient Greek'
    elif mod_cnt > kath_cnt * 1.3 and mod_cnt > anc_cnt * 1.3:
        return 'Modern Greek'
    else:
        return 'Katharevousa'


def classify_genre(subjects: list[str]) -> str:
    """Classify book subjects into a broad genre."""
    subjects_lower = [s.lower() for s in subjects]
    
    genre_mappings = {
        "Poetry": ["poetry", "poems", "verse"],
        "Drama": ["drama", "plays", "tragedies", "comedies", "tragedy", "comedy"],
        "Fiction": ["fiction", "novels", "novel", "stories", "short stories", "tales", "fairy tales"],
        "Philosophy": ["philosophy", "philosophical", "ethics"],
        "History": ["history", "biography", "historical"],
        "Religion": ["religion", "bible", "theology", "religious", "christianity"],
    }
    
    for genre, keywords in genre_mappings.items():
        for sub in subjects_lower:
            if any(kw in sub for kw in keywords):
                return genre
                
    return "Miscellaneous"

# ---------------------------------------------------------------------------
# Gutenberg Downloader
# ---------------------------------------------------------------------------

def download_gutenberg() -> None:
    """Query Gutendex API for all Greek books, download them, and classify them."""
    print("\n=== Downloading Project Gutenberg Greek Corpus ===")
    url = "https://gutendex.com/books?languages=el"
    books = []
    
    # 1. Fetch metadata catalog
    while url:
        print(f"Fetching catalog page: {url}")
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            books.extend(data.get("results", []))
            url = data.get("next")
        except Exception as exc:
            print(f"Error fetching catalog page: {exc}")
            break
            
    print(f"Found {len(books)} Greek books in Project Gutenberg catalog.")
    
    # 2. Download and Classify each book
    catalog = []
    books_txt_dir = GUTENBERG_DIR / "books"
    books_txt_dir.mkdir(exist_ok=True)
    
    for i, book in enumerate(books, 1):
        book_id = book.get("id")
        title = book.get("title")
        authors = [a.get("name", "Unknown") for a in book.get("authors", [])]
        subjects = book.get("subjects", [])
        
        print(f"[{i}/{len(books)}] Processing Book #{book_id}: {title[:50]}...")
        
        # Find plain text download URL
        formats = book.get("formats", {})
        txt_url = None
        for fmt, link in formats.items():
            if "text/plain" in fmt:
                txt_url = link
                break
                
        if not txt_url:
            print(f"  ✗ No plain text format found for book {book_id}. Skipping.")
            continue
            
        dest_file = books_txt_dir / f"gutenberg_{book_id}.txt.gz"
        text_content = None
        
        # Check if already downloaded
        if dest_file.exists():
            print(f"  → Found cached version on disk. Skipping download.")
            try:
                with gzip.open(dest_file, "rt", encoding="utf-8") as f:
                    full_content = f.read()
                
                # Check and heal mojibake
                healed_content = heal_text_mojibake(full_content)
                if healed_content != full_content:
                    print(f"    [!] Healed Mojibake in cached file for book {book_id}. Overwriting cache.")
                    with gzip.open(dest_file, "wt", encoding="utf-8") as f_out:
                        f_out.write(healed_content)
                    full_content = healed_content
                
                text_content = full_content
            except Exception as exc:
                print(f"  ✗ Failed to read cached file: {exc}. Re-downloading.")
                text_content = None
                
        if text_content is None:
            # Download text content
            try:
                txt_resp = requests.get(txt_url, timeout=30)
                txt_resp.raise_for_status()
                raw_text = txt_resp.text
                
                # Heal mojibake on download
                text_content = heal_text_mojibake(raw_text)
                if text_content != raw_text:
                    print(f"    [!] Healed Mojibake in downloaded text for book {book_id}.")
                
                # Save compressed text file
                with gzip.open(dest_file, "wt", encoding="utf-8") as f:
                    f.write(text_content)
            except Exception as exc:
                print(f"  ✗ Failed to download/save text for book {book_id}: {exc}")
                continue
            
        # Classify variety and genre
        variety = classify_greek_variety(text_content[:50000], authors)
        genre = classify_genre(subjects)
        
        catalog.append({
            "id": book_id,
            "title": title,
            "authors": authors,
            "subjects": subjects,
            "variety": variety,
            "genre": genre,
            "file": f"gutenberg/books/gutenberg_{book_id}.txt.gz"
        })
        print(f"  ✓ Saved & Classified: {variety} | Genre: {genre}")
        
    # Save the catalog
    catalog_path = GUTENBERG_DIR / "gutenberg_catalog.json"
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Gutenberg catalog saved to {catalog_path}")

# ---------------------------------------------------------------------------
# BabyLM Downloader
# ---------------------------------------------------------------------------

def download_babylm() -> None:
    """Download BabyLM-ell dataset from HF and save as compressed JSONL."""
    print("\n=== Downloading BabyLM-ell Dataset ===")
    try:
        from datasets import load_dataset
    except ImportError:
        print("  ✗ 'datasets' library is missing. Install via pip.")
        return
        
    try:
        print("Loading BabyLM-ell dataset from Hugging Face...")
        dataset = load_dataset("BabyLM-community/babylm-ell", split="train")
        
        output_file = BABYLM_DIR / "babylm_ell.jsonl.gz"
        print(f"Writing to {output_file}...")
        
        total_docs = 0
        with gzip.open(output_file, "wt", encoding="utf-8") as f:
            for item in dataset:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                total_docs += 1
                
        print(f"✓ BabyLM-ell download complete: {total_docs} documents written.")
    except Exception as exc:
        print(f"  ✗ Failed to download BabyLM-ell: {exc}")

# ---------------------------------------------------------------------------
# Interwar Poetry Downloader
# ---------------------------------------------------------------------------

def download_poetry() -> None:
    """Download and extract the Interwar Greek Poetry repository."""
    print("\n=== Downloading Interwar Greek Poetry Corpus ===")
    zip_url = "https://github.com/StergiosCha/RAG-poetry/archive/refs/heads/main.zip"
    zip_dest = POETRY_DIR / "rag_poetry.zip"
    
    try:
        print("Downloading repository zip...")
        resp = requests.get(zip_url, timeout=30)
        resp.raise_for_status()
        with open(zip_dest, "wb") as f:
            f.write(resp.content)
            
        print("Extracting files...")
        extract_tmp = POETRY_DIR / "temp_extract"
        extract_tmp.mkdir(exist_ok=True)
        
        with zipfile.ZipFile(zip_dest, "r") as zip_ref:
            zip_ref.extractall(extract_tmp)
            
        # Move .txt files to poetry/ and cleanup
        poems_dir = list(extract_tmp.glob("RAG-poetry-*"))[0]
        data_dir = poems_dir / "Data"
        count = 0
        
        # Fallback to root of repo if Data directory is not found
        search_dir = data_dir if data_dir.exists() else poems_dir
        
        for txt_file in search_dir.glob("*.txt"):
            shutil.move(str(txt_file), str(POETRY_DIR / txt_file.name))
            count += 1
            
        # Cleanup
        shutil.rmtree(extract_tmp)
        zip_dest.unlink()
        
        print(f"✓ Extracted {count} interwar poems directly to {POETRY_DIR}/")
    except Exception as exc:
        print(f"  ✗ Failed to download/extract poetry: {exc}")

# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    download_gutenberg()
    download_babylm()
    download_poetry()
    print("\n=== All Downloads Complete ===")
