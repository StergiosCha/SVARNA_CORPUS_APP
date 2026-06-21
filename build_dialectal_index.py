#!/usr/bin/env python3
"""
Build a SQLite FTS5 index from the GRDD+ dialectal corpus.

Reads 9 dialect text files from grdd_plus/ and creates dialectal.db
with the SAME schema as corpus.db so the existing Greek Corpus Workbench
app can query it without code changes.

Usage:
    python build_dialectal_index.py [--output dialectal.db]
"""

import argparse
import json
import os
import re
import sqlite3
import time

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GRDD_DIR = os.path.join(SCRIPT_DIR, "grdd_plus")

# ---------------------------------------------------------------------------
# Dialect mapping from filename to clean name
# ---------------------------------------------------------------------------

DIALECT_MAP = {
    'Cretan_final.txt': ('cretan', 'Cretan'),
    'final_cypriot.txt': ('cypriot', 'Cypriot'),
    'final_katharevousa.txt': ('katharevousa', 'Katharevousa'),
    'Pontic_final.txt': ('pontic', 'Pontic'),
    'final_tsakonian.txt': ('tsakonian', 'Tsakonian'),
    'Griko_final.txt': ('griko', 'Griko (Southern Italian Greek)'),
    'Northern_final.txt': ('northern', 'Northern Greek'),
    'Eptanisian_final.txt': ('eptanisian', 'Eptanisian (Ionian)'),
    'final_maniot.txt': ('maniot', 'Maniot'),
}

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
# Processor
# ---------------------------------------------------------------------------

def process_grdd(inserter):
    """Process all GRDD+ dialect text files."""
    if not os.path.isdir(GRDD_DIR):
        print(f"  [SKIP] GRDD+ directory not found: {GRDD_DIR}")
        return

    txt_files = [f for f in os.listdir(GRDD_DIR) if f.endswith('.txt')]
    if not txt_files:
        print(f"  [SKIP] No .txt files in {GRDD_DIR}")
        return

    print(f"  Found {len(txt_files)} dialect files")

    for txt_file in sorted(txt_files):
        mapping = DIALECT_MAP.get(txt_file)
        if not mapping:
            print(f"    [SKIP] Unknown file: {txt_file}")
            continue

        slug, display_name = mapping
        corpus_name = f"grdd_{slug}"
        filepath = os.path.join(GRDD_DIR, txt_file)

        t0 = time.time()
        count = 0

        metadata = json.dumps({
            'dialect': display_name,
            'source': 'GRDD+',
        }, ensure_ascii=False)

        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if len(line) >= 3:
                    inserter.add(
                        line, corpus_name, 'dialectal', 'written',
                        '', metadata
                    )
                    count += 1

        elapsed = time.time() - t0
        print(f"    {display_name:<35} {count:>8,} lines [{elapsed:.1f}s]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Build dialectal.db FTS5 index from GRDD+ dialect corpus.'
    )
    parser.add_argument(
        '--output', default=os.path.join(SCRIPT_DIR, 'dialectal.db'),
        help='Path for the output SQLite database file.'
    )
    args = parser.parse_args()

    output_path = os.path.abspath(args.output)
    print(f"GRDD+ directory: {GRDD_DIR}")
    print(f"Output database: {output_path}")
    print()

    total_t0 = time.time()

    print("Creating database...")
    conn = create_database(output_path)
    inserter = Inserter(conn)

    print()
    print("=" * 60)
    print("Processing GRDD+ dialectal corpora")
    print("=" * 60)
    print()

    process_grdd(inserter)

    # Finalize
    print()
    print("=" * 60)
    print("Finalizing...")
    print("=" * 60)
    inserter.finalize()

    # Print statistics
    print()
    print("=" * 60)
    print("Dialectal Corpus Statistics")
    print("=" * 60)
    print(f"{'Corpus':<30} {'Register':<15} {'Mode':<10} {'Sentences':>10} {'Tokens':>12}")
    print("-" * 77)

    total_sentences = 0
    total_tokens = 0

    cursor = conn.execute(
        "SELECT corpus, register, mode, sentence_count, token_count "
        "FROM corpus_stats ORDER BY token_count DESC"
    )
    for row in cursor:
        corpus, register, mode, sent_count, tok_count = row
        print(f"{corpus:<30} {register:<15} {mode:<10} {sent_count:>10,} {tok_count:>12,}")
        total_sentences += sent_count
        total_tokens += tok_count

    print("-" * 77)
    print(f"{'TOTAL':<30} {'':<15} {'':<10} {total_sentences:>10,} {total_tokens:>12,}")

    total_elapsed = time.time() - total_t0
    db_size_mb = os.path.getsize(output_path) / (1024 * 1024)

    print()
    print(f"Total time    : {total_elapsed:.0f}s")
    print(f"Database size : {db_size_mb:.1f} MB")
    print(f"Total rows    : {inserter.total_inserted:,}")
    print()
    print("Done.")

    conn.close()


if __name__ == '__main__':
    main()
