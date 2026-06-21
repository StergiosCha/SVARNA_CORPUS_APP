#!/usr/bin/env python3
"""
Build a SQLite FTS5 index from Greek literature corpora.

Reads three corpora:
  1. Project Gutenberg Greek books (gzip-compressed, with catalog.json)
  2. BabyLM-ell (JSONL.gz from Hugging Face)
  3. Interwar Greek Poetry (plain text files per poet)

Creates literature.db with the SAME schema as corpus.db so the existing
Greek Corpus Workbench app can query it without code changes.

Usage:
    python build_literature_index.py [--output literature.db]
"""

import argparse
import gzip
import json
import os
import re
import sqlite3
import time

# ---------------------------------------------------------------------------
# Paths (relative to this script)
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GUTENBERG_DIR = os.path.join(SCRIPT_DIR, "gutenberg")
BABYLM_DIR = os.path.join(SCRIPT_DIR, "babylm")
POETRY_DIR = os.path.join(SCRIPT_DIR, "poetry")

# ---------------------------------------------------------------------------
# Sentence splitting (same logic as build_index.py)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(
    r'[.!;·;]'
    r'(?=\s|$)'
)


def split_sentences(text):
    """Split text into sentences on Greek punctuation boundaries."""
    if not text:
        return []
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    sentences = []
    for part in parts:
        s = part.strip()
        if len(s) >= 3:
            sentences.append(s)
    if not sentences and len(text) >= 3:
        sentences = [text]
    return sentences


# ---------------------------------------------------------------------------
# Gutenberg header/footer stripping
# ---------------------------------------------------------------------------

_GUT_START = re.compile(
    r'\*\*\*\s*START OF (?:THE |THIS )?PROJECT GUTENBERG',
    re.IGNORECASE
)
_GUT_END = re.compile(
    r'\*\*\*\s*END OF (?:THE |THIS )?PROJECT GUTENBERG',
    re.IGNORECASE
)


def strip_gutenberg_boilerplate(text):
    """Remove Project Gutenberg header and footer from a text."""
    lines = text.split('\n')

    start_idx = 0
    end_idx = len(lines)

    for i, line in enumerate(lines):
        if _GUT_START.search(line):
            start_idx = i + 1
            break

    for i in range(len(lines) - 1, -1, -1):
        if _GUT_END.search(lines[i]):
            end_idx = i
            break

    return '\n'.join(lines[start_idx:end_idx]).strip()


# ---------------------------------------------------------------------------
# Database helpers (identical schema to corpus.db)
# ---------------------------------------------------------------------------

def create_database(db_path):
    """Create a fresh SQLite database with the FTS5 table and stats table."""
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")

    conn.execute("""
        CREATE VIRTUAL TABLE sentences USING fts5(
            text,
            corpus,
            register,
            mode,
            year,
            metadata,
            tokenize='unicode61'
        )
    """)

    conn.execute("""
        CREATE TABLE corpus_stats (
            corpus TEXT PRIMARY KEY,
            register TEXT,
            mode TEXT,
            sentence_count INTEGER,
            token_count INTEGER
        )
    """)

    conn.commit()
    return conn


BATCH_SIZE = 5000


class Inserter:
    """Batched inserter for the sentences FTS5 table."""

    def __init__(self, conn):
        self.conn = conn
        self.buffer = []
        self.total_inserted = 0
        self.stats = {}

    def add(self, text, corpus, register, mode, year='', metadata=''):
        text = text.strip()
        if len(text) < 3:
            return
        self.buffer.append((text, corpus, register, mode, str(year), metadata))
        token_count = len(text.split())
        if corpus not in self.stats:
            self.stats[corpus] = [register, mode, 0, 0]
        self.stats[corpus][2] += 1
        self.stats[corpus][3] += token_count

        if len(self.buffer) >= BATCH_SIZE:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        self.conn.executemany(
            "INSERT INTO sentences(text, corpus, register, mode, year, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            self.buffer
        )
        self.conn.commit()
        self.total_inserted += len(self.buffer)
        self.buffer = []

    def finalize(self):
        self.flush()
        for corpus, (register, mode, sent_count, tok_count) in self.stats.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO corpus_stats "
                "(corpus, register, mode, sentence_count, token_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (corpus, register, mode, sent_count, tok_count)
            )
        self.conn.commit()


# ---------------------------------------------------------------------------
# Corpus processors
# ---------------------------------------------------------------------------

def process_gutenberg(inserter):
    """Process Project Gutenberg Greek books using the catalog."""
    catalog_path = os.path.join(GUTENBERG_DIR, "gutenberg_catalog.json")
    books_dir = os.path.join(GUTENBERG_DIR, "books")

    if not os.path.exists(catalog_path):
        print(f"  [SKIP] Gutenberg catalog not found: {catalog_path}")
        return

    with open(catalog_path, 'r', encoding='utf-8') as f:
        catalog = json.load(f)

    print(f"  Found {len(catalog)} books in catalog")

    # Map variety to corpus name
    variety_map = {
        'Ancient Greek': 'gutenberg_ancient',
        'Katharevousa': 'gutenberg_katharevousa',
        'Modern Greek': 'gutenberg_modern',
    }

    # Map genre to register
    genre_register_map = {
        'Poetry': 'literary_poetry',
        'Drama': 'literary_drama',
        'Fiction': 'literary_fiction',
        'Philosophy': 'literary_philosophy',
        'History': 'literary_history',
        'Religion': 'literary_religion',
        'Miscellaneous': 'literary_misc',
    }

    processed = 0
    t0 = time.time()

    for entry in catalog:
        book_id = entry['id']
        title = entry.get('title', '')
        authors = entry.get('authors', [])
        variety = entry.get('variety', 'Modern Greek')
        genre = entry.get('genre', 'Miscellaneous')

        gz_path = os.path.join(SCRIPT_DIR, entry.get('file', ''))
        if not os.path.exists(gz_path):
            # Try alternative path
            gz_path = os.path.join(books_dir, f"gutenberg_{book_id}.txt.gz")
        if not os.path.exists(gz_path):
            continue

        try:
            with gzip.open(gz_path, 'rt', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception as e:
            print(f"    [ERROR] Book {book_id}: {e}")
            continue

        # Strip Gutenberg boilerplate
        text = strip_gutenberg_boilerplate(text)
        if len(text) < 50:
            continue

        corpus_name = variety_map.get(variety, 'gutenberg_modern')
        register = genre_register_map.get(genre, 'literary_misc')

        metadata = json.dumps({
            'title': title,
            'authors': authors,
            'variety': variety,
            'genre': genre,
            'gutenberg_id': book_id,
        }, ensure_ascii=False)

        sentences = split_sentences(text)
        for sent in sentences:
            inserter.add(sent, corpus_name, register, 'written', '', metadata)

        processed += 1

    elapsed = time.time() - t0
    total_sents = sum(
        inserter.stats.get(c, [0, 0, 0, 0])[2]
        for c in ['gutenberg_ancient', 'gutenberg_katharevousa', 'gutenberg_modern']
    )
    print(f"  Gutenberg: {processed} books, {total_sents:,} sentences [{elapsed:.1f}s]")


def process_babylm(inserter):
    """Process BabyLM-ell compressed JSONL."""
    jsonl_path = os.path.join(BABYLM_DIR, "babylm_ell.jsonl.gz")

    if not os.path.exists(jsonl_path):
        print(f"  [SKIP] BabyLM file not found: {jsonl_path}")
        return

    print(f"  Processing BabyLM-ell...")
    t0 = time.time()
    doc_count = 0

    # Category to register mapping
    category_register = {
        'child-available-speech': 'spoken',
        'child-directed-speech': 'spoken',
        'child-speech': 'spoken',
        'written-ebooks': 'literary_fiction',
        'written-news': 'news',
        'written-web': 'web',
        'written-subtitles': 'dialogue',
        'written-wikipedia': 'encyclopedic',
    }

    with gzip.open(jsonl_path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            text = obj.get('text', '')
            if not text or len(text) < 10:
                continue

            category = obj.get('category', 'unknown')
            data_source = obj.get('data-source', '')
            age_est = obj.get('age-estimate', '')

            # Determine corpus and register
            corpus_name = f"babylm_{category.replace('-', '_')}"
            register = category_register.get(category, 'mixed')

            # Determine mode
            if 'speech' in category:
                mode = 'spoken-like'
            else:
                mode = 'written'

            metadata = json.dumps({
                'category': category,
                'data_source': data_source,
                'age_estimate': age_est,
            }, ensure_ascii=False)

            sentences = split_sentences(text)
            for sent in sentences:
                inserter.add(sent, corpus_name, register, mode, '', metadata)

            doc_count += 1
            if doc_count % 2000 == 0:
                elapsed = time.time() - t0
                print(f"    babylm: {doc_count:,} docs [{elapsed:.1f}s]")

    elapsed = time.time() - t0
    babylm_sents = sum(
        v[2] for k, v in inserter.stats.items() if k.startswith('babylm_')
    )
    print(f"  BabyLM: {doc_count:,} docs, {babylm_sents:,} sentences [{elapsed:.1f}s]")


def process_poetry(inserter):
    """Process Interwar Greek Poetry plain text files."""
    if not os.path.isdir(POETRY_DIR):
        print(f"  [SKIP] Poetry directory not found: {POETRY_DIR}")
        return

    txt_files = [f for f in os.listdir(POETRY_DIR) if f.endswith('.txt')]
    if not txt_files:
        print(f"  [SKIP] No .txt files in {POETRY_DIR}")
        return

    print(f"  Processing {len(txt_files)} poetry files...")
    t0 = time.time()
    total_lines = 0

    for txt_file in sorted(txt_files):
        poet_name = os.path.splitext(txt_file)[0]
        # Convert CamelCase to readable: TellosAgras -> Tellos Agras
        readable_name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', poet_name)

        filepath = os.path.join(POETRY_DIR, txt_file)
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        metadata = json.dumps({
            'poet': readable_name,
            'period': 'interwar (1920-1940)',
        }, ensure_ascii=False)

        # For poetry, split by stanza (double newline) then by line
        # Poems are better indexed as verse lines or stanzas, not sentences
        stanzas = re.split(r'\n\s*\n', text)
        for stanza in stanzas:
            stanza = stanza.strip()
            if len(stanza) < 5:
                continue
            # Each stanza as one entry (preserves poetic context)
            inserter.add(
                stanza, 'interwar_poetry', 'literary_poetry',
                'written', '1930', metadata
            )
            total_lines += 1

    elapsed = time.time() - t0
    poetry_sents = inserter.stats.get('interwar_poetry', [0, 0, 0, 0])[2]
    print(f"  Poetry: {len(txt_files)} poets, {poetry_sents:,} stanzas [{elapsed:.1f}s]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Build literature.db FTS5 index from Greek literature corpora.'
    )
    parser.add_argument(
        '--output', default=os.path.join(SCRIPT_DIR, 'literature.db'),
        help='Path for the output SQLite database file.'
    )
    args = parser.parse_args()

    output_path = os.path.abspath(args.output)
    print(f"Literature directory: {SCRIPT_DIR}")
    print(f"Output database:     {output_path}")
    print()

    total_t0 = time.time()

    # Create database
    print("Creating database...")
    conn = create_database(output_path)
    inserter = Inserter(conn)

    # Process each corpus
    print()
    print("=" * 60)
    print("Processing literature corpora")
    print("=" * 60)
    print()

    print("[1/3] Project Gutenberg Greek")
    process_gutenberg(inserter)
    print()

    print("[2/3] BabyLM-ell")
    process_babylm(inserter)
    print()

    print("[3/3] Interwar Greek Poetry")
    process_poetry(inserter)

    # Finalize
    print()
    print("=" * 60)
    print("Finalizing...")
    print("=" * 60)
    inserter.finalize()

    # Print statistics
    print()
    print("=" * 60)
    print("Literature Corpus Statistics")
    print("=" * 60)
    print(f"{'Corpus':<35} {'Register':<20} {'Mode':<15} {'Sentences':>10} {'Tokens':>12}")
    print("-" * 92)

    total_sentences = 0
    total_tokens = 0

    cursor = conn.execute(
        "SELECT corpus, register, mode, sentence_count, token_count "
        "FROM corpus_stats ORDER BY corpus"
    )
    for row in cursor:
        corpus, register, mode, sent_count, tok_count = row
        print(f"{corpus:<35} {register:<20} {mode:<15} {sent_count:>10,} {tok_count:>12,}")
        total_sentences += sent_count
        total_tokens += tok_count

    print("-" * 92)
    print(f"{'TOTAL':<35} {'':<20} {'':<15} {total_sentences:>10,} {total_tokens:>12,}")

    total_elapsed = time.time() - total_t0
    db_size_mb = os.path.getsize(output_path) / (1024 * 1024)

    print()
    print(f"Total time    : {total_elapsed:.0f}s ({total_elapsed/60:.1f}m)")
    print(f"Database size : {db_size_mb:.1f} MB")
    print(f"Total rows    : {inserter.total_inserted:,}")
    print()
    print("Done.")

    conn.close()


if __name__ == '__main__':
    main()
