#!/usr/bin/env python3
"""
Build a SQLite FTS5 index from Greek corpora.

Reads multiple Greek text corpora in different formats and loads them into
a single SQLite database with FTS5 full-text search support.

Usage:
    python build_index.py --data-dir /path/to/greek_corpora --output /path/to/corpus.db
"""

import argparse
import csv
import glob
import gzip
import json
import os
import re
import sqlite3
import sys
import lzma
import tarfile
import time

# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

# Split on sentence-ending punctuation: period (.), exclamation (!),
# Greek question mark (;), Latin semicolon used as question mark (;),
# and middle dot (·).  We require the punctuation to be followed by
# whitespace or end-of-string so abbreviations like "π.χ." or "κ.λπ."
# don't cause spurious splits (since the period is followed immediately
# by another letter rather than a space).
_SENTENCE_SPLIT_RE = re.compile(
    r'[.!;·;]'            # sentence-ending punctuation
    r'(?=\s|$)'            # followed by whitespace or end of string
)

def split_sentences(text):
    """Split text into sentences on Greek punctuation boundaries."""
    if not text:
        return []
    # Normalize whitespace
    text = text.strip()
    if not text:
        return []

    parts = _SENTENCE_SPLIT_RE.split(text)
    sentences = []
    for part in parts:
        s = part.strip()
        if len(s) >= 3:
            sentences.append(s)
    # If no split happened but the text is long enough, return as-is
    if not sentences and len(text) >= 3:
        sentences = [text]
    return sentences


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def create_database(db_path):
    """Create a fresh SQLite database with the FTS5 table and stats table."""
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64 MB cache

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


BATCH_SIZE = 10000

class Inserter:
    """Batched inserter for the sentences FTS5 table."""

    def __init__(self, conn):
        self.conn = conn
        self.buffer = []
        self.total_inserted = 0
        # Per-corpus stats: {corpus: [register, mode, sentence_count, token_count]}
        self.stats = {}

    def add(self, text, corpus, register, mode, year='', metadata=''):
        """Add a sentence to the insert buffer."""
        text = text.strip()
        if len(text) < 3:
            return
        self.buffer.append((text, corpus, register, mode, str(year), metadata))
        # Update stats
        token_count = len(text.split())
        if corpus not in self.stats:
            self.stats[corpus] = [register, mode, 0, 0]
        self.stats[corpus][2] += 1
        self.stats[corpus][3] += token_count

        if len(self.buffer) >= BATCH_SIZE:
            self.flush()

    def flush(self):
        """Write buffered rows to the database."""
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
        """Flush remaining rows and write corpus_stats."""
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

def process_parliament(data_dir, inserter):
    """Process Greek parliament proceedings CSV."""
    csv_path = os.path.join(
        data_dir, 'parliament', 'cleaned', 'dataset_versions', 'tell_all_cleaned.csv'
    )
    if not os.path.exists(csv_path):
        print(f"  [SKIP] Parliament file not found: {csv_path}")
        return

    print("  Processing parliament corpus...")
    csv.field_size_limit(sys.maxsize)

    count = 0
    limit = 200_000
    t0 = time.time()

    with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader)
        # Locate column indices
        col_map = {name: idx for idx, name in enumerate(header)}

        for row in reader:
            if count >= limit:
                break
            try:
                speech = row[col_map['speech']]
                member_name = row[col_map['member_name']]
                sitting_date = row[col_map['sitting_date']]
                political_party = row[col_map['political_party']]
                member_gender = row[col_map['member_gender']]
            except (IndexError, KeyError):
                continue

            # Extract year from sitting_date (format DD/MM/YYYY)
            year = ''
            if sitting_date:
                parts = sitting_date.split('/')
                if len(parts) == 3:
                    year = parts[2]
                else:
                    # Try ISO format YYYY-MM-DD
                    parts = sitting_date.split('-')
                    if len(parts) == 3 and len(parts[0]) == 4:
                        year = parts[0]

            metadata = json.dumps({
                'speaker': member_name,
                'party': political_party,
                'gender': member_gender
            }, ensure_ascii=False)

            sentences = split_sentences(speech)
            for sent in sentences:
                inserter.add(sent, 'parliament', 'political', 'transcribed',
                             year, metadata)

            count += 1
            if count % 50000 == 0:
                elapsed = time.time() - t0
                print(f"    parliament: {count:,} speeches processed "
                      f"({inserter.stats.get('parliament', [0,0,0,0])[2]:,} sentences) "
                      f"[{elapsed:.0f}s]")

    elapsed = time.time() - t0
    sent_count = inserter.stats.get('parliament', [0, 0, 0, 0])[2]
    print(f"    parliament: done — {count:,} speeches, {sent_count:,} sentences [{elapsed:.0f}s]")


def process_opensubtitles(data_dir, inserter):
    """Process OpenSubtitles gzipped text file."""
    gz_path = os.path.join(data_dir, 'opensubtitles', 'el.txt.gz')
    if not os.path.exists(gz_path):
        print(f"  [SKIP] OpenSubtitles file not found: {gz_path}")
        return

    print("  Processing opensubtitles corpus...")
    count = 0
    limit = 2_000_000
    t0 = time.time()

    with gzip.open(gz_path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            if count >= limit:
                break
            line = line.strip()
            if len(line) >= 3:
                inserter.add(line, 'opensubtitles', 'dialogue', 'spoken-like')
                count += 1
            if count % 500000 == 0 and count > 0:
                elapsed = time.time() - t0
                print(f"    opensubtitles: {count:,} lines [{elapsed:.0f}s]")

    elapsed = time.time() - t0
    print(f"    opensubtitles: done — {count:,} lines [{elapsed:.0f}s]")


def process_wikipedia(data_dir, inserter):
    """Process Wikipedia CirrusSearch JSON dump."""
    gz_path = os.path.join(
        data_dir, 'wikipedia', 'elwiki-20251229-cirrussearch-content.json.gz'
    )
    if not os.path.exists(gz_path):
        print(f"  [SKIP] Wikipedia file not found: {gz_path}")
        return

    print("  Processing wikipedia corpus...")
    article_count = 0
    limit = 100_000
    t0 = time.time()

    with gzip.open(gz_path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            if article_count >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Skip index lines (they have an "index" key at top level)
            if 'index' in obj:
                continue

            # Data lines have "title" and "text"
            text = obj.get('text', '')
            title = obj.get('title', '')
            if not text:
                continue

            metadata = json.dumps({'title': title}, ensure_ascii=False) if title else ''

            sentences = split_sentences(text)
            for sent in sentences:
                inserter.add(sent, 'wikipedia', 'encyclopedic', 'written',
                             metadata=metadata)

            article_count += 1
            if article_count % 25000 == 0:
                elapsed = time.time() - t0
                sent_count = inserter.stats.get('wikipedia', [0, 0, 0, 0])[2]
                print(f"    wikipedia: {article_count:,} articles "
                      f"({sent_count:,} sentences) [{elapsed:.0f}s]")

    elapsed = time.time() - t0
    sent_count = inserter.stats.get('wikipedia', [0, 0, 0, 0])[2]
    print(f"    wikipedia: done — {article_count:,} articles, "
          f"{sent_count:,} sentences [{elapsed:.0f}s]")


def process_leipzig(data_dir, inserter):
    """Process Leipzig corpora tar.gz files."""
    leipzig_dir = os.path.join(data_dir, 'leipzig')
    if not os.path.isdir(leipzig_dir):
        print(f"  [SKIP] Leipzig directory not found: {leipzig_dir}")
        return

    print("  Processing Leipzig corpora...")
    tar_files = sorted(glob.glob(os.path.join(leipzig_dir, '*.tar.gz')))

    for tar_path in tar_files:
        basename = os.path.basename(tar_path)
        # Skip zero-byte files
        if os.path.getsize(tar_path) == 0:
            print(f"    [SKIP] {basename} (empty file)")
            continue

        # Determine register from filename
        name_lower = basename.lower()
        if 'newscrawl' in name_lower or 'news' in name_lower:
            register = 'news'
        elif 'wikipedia' in name_lower:
            register = 'encyclopedic'
        elif 'mixed' in name_lower:
            register = 'mixed'
        else:
            register = 'mixed'

        # Extract year from filename: ell_news_2020_1M -> 2020
        year_match = re.search(r'_(\d{4})_', basename)
        year = year_match.group(1) if year_match else ''

        corpus_name = f"leipzig_{basename.replace('.tar.gz', '')}"

        print(f"    Processing {basename} (register={register}, year={year})...")
        t0 = time.time()
        count = 0

        try:
            with tarfile.open(tar_path, 'r:gz') as tf:
                for member in tf.getmembers():
                    if member.name.endswith('-sentences.txt'):
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        for raw_line in f:
                            try:
                                line = raw_line.decode('utf-8', errors='replace')
                            except Exception:
                                continue
                            # Tab-separated: id\tsentence
                            parts = line.strip().split('\t', 1)
                            if len(parts) < 2:
                                continue
                            sentence = parts[1].strip()
                            if len(sentence) >= 3:
                                inserter.add(sentence, corpus_name, register,
                                             'written', year)
                                count += 1
                        break  # Only process the sentences file
        except Exception as e:
            print(f"    [ERROR] Failed to process {basename}: {e}")
            continue

        elapsed = time.time() - t0
        print(f"    {basename}: {count:,} sentences [{elapsed:.0f}s]")


def process_europarl(data_dir, inserter):
    """Process Europarl gzipped text file."""
    gz_path = os.path.join(data_dir, 'europarl', 'europarl-el.txt.gz')
    if not os.path.exists(gz_path):
        print(f"  [SKIP] Europarl file not found: {gz_path}")
        return

    print("  Processing europarl corpus...")
    count = 0
    t0 = time.time()

    with gzip.open(gz_path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if len(line) >= 3:
                inserter.add(line, 'europarl', 'institutional', 'written')
                count += 1
            if count % 500000 == 0 and count > 0:
                elapsed = time.time() - t0
                print(f"    europarl: {count:,} lines [{elapsed:.0f}s]")

    elapsed = time.time() - t0
    print(f"    europarl: done — {count:,} lines [{elapsed:.0f}s]")


def process_universal_dependencies(data_dir, inserter):
    """Process Universal Dependencies CoNLL-U files."""
    ud_dir = os.path.join(data_dir, 'universal_dependencies')
    if not os.path.isdir(ud_dir):
        print(f"  [SKIP] Universal Dependencies directory not found: {ud_dir}")
        return

    print("  Processing Universal Dependencies corpora...")

    treebanks = [
        ('UD_Greek-GDT', os.path.join(ud_dir, 'UD_Greek-GDT', 'UD_Greek-GDT-master')),
        ('UD_Greek-GUD', os.path.join(ud_dir, 'UD_Greek-GUD', 'UD_Greek-GUD-master')),
    ]

    for tb_name, tb_dir in treebanks:
        if not os.path.isdir(tb_dir):
            print(f"    [SKIP] Treebank directory not found: {tb_dir}")
            continue

        conllu_files = sorted(glob.glob(os.path.join(tb_dir, '*.conllu')))
        if not conllu_files:
            print(f"    [SKIP] No .conllu files in {tb_dir}")
            continue

        corpus_name = f"ud_{tb_name.lower()}"
        count = 0
        t0 = time.time()

        for conllu_path in conllu_files:
            with open(conllu_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if line.startswith('# text = '):
                        sentence = line[len('# text = '):].strip()
                        if len(sentence) >= 3:
                            inserter.add(sentence, corpus_name, 'mixed', 'written')
                            count += 1

        elapsed = time.time() - t0
        print(f"    {tb_name}: {count:,} sentences [{elapsed:.0f}s]")


def process_cc100(data_dir, inserter):
    """Process CC-100 Greek corpus (xz-compressed plain text, one sentence per line)."""
    xz_path = os.path.join(data_dir, 'cc100', 'el.txt.xz')
    if not os.path.exists(xz_path):
        print(f"  [SKIP] CC-100 file not found: {xz_path}")
        return

    print("  Processing CC-100 corpus...")
    count = 0
    limit = 5_000_000  # 5M sentences (CC-100 is huge)
    t0 = time.time()

    with lzma.open(xz_path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            if count >= limit:
                break
            line = line.strip()
            if len(line) >= 3:
                inserter.add(line, 'cc100', 'web', 'written')
                count += 1
            if count % 1000000 == 0 and count > 0:
                elapsed = time.time() - t0
                print(f"    cc100: {count:,} lines [{elapsed:.0f}s]")

    elapsed = time.time() - t0
    print(f"    cc100: done — {count:,} lines [{elapsed:.0f}s]")


def process_opus(data_dir, inserter):
    """Process OPUS subcorpora (plain text or gzipped, one sentence per line)."""
    opus_dir = os.path.join(data_dir, 'opus')
    if not os.path.isdir(opus_dir):
        print(f"  [SKIP] OPUS directory not found: {opus_dir}")
        return

    print("  Processing OPUS corpora...")

    # Register mapping for each subcorpus
    subcorpora = {
        'CCAligned': ('web', 'written'),
        'EUbookshop': ('institutional', 'written'),
        'ParaCrawl': ('web', 'written'),
        'WikiMatrix': ('encyclopedic', 'written'),
    }

    for sub_name, (register, mode) in subcorpora.items():
        sub_dir = os.path.join(opus_dir, sub_name)
        if not os.path.isdir(sub_dir):
            print(f"    [SKIP] {sub_name} not found")
            continue

        # Prefer plain text, fall back to gzipped
        txt_path = os.path.join(sub_dir, 'el.txt')
        gz_path = os.path.join(sub_dir, 'el.txt.gz')

        if os.path.exists(txt_path) and os.path.getsize(txt_path) > 0:
            open_fn = lambda p=txt_path: open(p, 'r', encoding='utf-8', errors='replace')
        elif os.path.exists(gz_path) and os.path.getsize(gz_path) > 0:
            open_fn = lambda p=gz_path: gzip.open(p, 'rt', encoding='utf-8', errors='replace')
        else:
            print(f"    [SKIP] {sub_name}: no el.txt or el.txt.gz found")
            continue

        corpus_name = f"opus_{sub_name.lower()}"
        limit = 2_000_000  # 2M lines per subcorpus
        count = 0
        t0 = time.time()

        print(f"    Processing {sub_name} (register={register})...")
        with open_fn() as f:
            for line in f:
                if count >= limit:
                    break
                line = line.strip()
                if len(line) >= 3:
                    inserter.add(line, corpus_name, register, mode)
                    count += 1
                if count % 500000 == 0 and count > 0:
                    elapsed = time.time() - t0
                    print(f"      {sub_name}: {count:,} lines [{elapsed:.0f}s]")

        elapsed = time.time() - t0
        print(f"      {sub_name}: done — {count:,} lines [{elapsed:.0f}s]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Build a SQLite FTS5 index from Greek corpora.'
    )
    parser.add_argument(
        '--data-dir', required=True,
        help='Path to the directory containing Greek corpora subdirectories.'
    )
    parser.add_argument(
        '--output', required=True,
        help='Path for the output SQLite database file.'
    )
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    output_path = os.path.abspath(args.output)

    if not os.path.isdir(data_dir):
        print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    print(f"Data directory : {data_dir}")
    print(f"Output database: {output_path}")
    print()

    total_t0 = time.time()

    # Create database
    print("Creating database...")
    conn = create_database(output_path)
    inserter = Inserter(conn)

    # Process each corpus
    print()
    print("=" * 60)
    print("Processing corpora")
    print("=" * 60)

    process_parliament(data_dir, inserter)
    print()
    process_opensubtitles(data_dir, inserter)
    print()
    process_wikipedia(data_dir, inserter)
    print()
    process_leipzig(data_dir, inserter)
    print()
    process_europarl(data_dir, inserter)
    print()
    process_universal_dependencies(data_dir, inserter)
    print()
    process_cc100(data_dir, inserter)
    print()
    process_opus(data_dir, inserter)

    # Finalize
    print()
    print("=" * 60)
    print("Finalizing...")
    print("=" * 60)
    inserter.finalize()

    # Print final statistics
    print()
    print("=" * 60)
    print("Corpus Statistics")
    print("=" * 60)
    print(f"{'Corpus':<40} {'Register':<15} {'Mode':<15} {'Sentences':>12} {'Tokens':>14}")
    print("-" * 96)

    total_sentences = 0
    total_tokens = 0

    cursor = conn.execute(
        "SELECT corpus, register, mode, sentence_count, token_count "
        "FROM corpus_stats ORDER BY corpus"
    )
    for row in cursor:
        corpus, register, mode, sent_count, tok_count = row
        print(f"{corpus:<40} {register:<15} {mode:<15} {sent_count:>12,} {tok_count:>14,}")
        total_sentences += sent_count
        total_tokens += tok_count

    print("-" * 96)
    print(f"{'TOTAL':<40} {'':<15} {'':<15} {total_sentences:>12,} {total_tokens:>14,}")

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
