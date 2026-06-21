#!/usr/bin/env python3
"""
Pre-compute speaker index from parliament metadata.
Extracts speaker/party/gender from JSON metadata and stores in a fast
lookup table with rowid references back to the FTS5 sentences table.

Run once after building the index:
    python3 precompute_speakers.py corpus.db
"""
import json
import sqlite3
import sys
import time


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 precompute_speakers.py <corpus.db>")
        sys.exit(1)

    db_path = sys.argv[1]
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create speaker_sentences table
    conn.execute("DROP TABLE IF EXISTS speaker_sentences")
    conn.execute("""
        CREATE TABLE speaker_sentences (
            rowid INTEGER PRIMARY KEY,
            speaker TEXT NOT NULL,
            party TEXT DEFAULT '',
            gender TEXT DEFAULT '',
            corpus TEXT DEFAULT '',
            register TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX idx_speaker ON speaker_sentences(speaker)")

    # Scan only rows with metadata containing 'speaker'
    # Use rowid to reference back to FTS5 table
    print("Scanning metadata for speaker info...")
    t0 = time.time()
    cursor = conn.execute(
        "SELECT rowid, metadata, corpus, register FROM sentences "
        "WHERE metadata IS NOT NULL AND metadata != ''"
    )

    batch = []
    total = 0
    skipped = 0
    for row in cursor:
        rid, meta_str, corpus, register = row
        try:
            meta = json.loads(meta_str)
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        speaker = meta.get("speaker", "").strip()
        if not speaker:
            skipped += 1
            continue
        party = meta.get("party", "")
        gender = meta.get("gender", "")
        batch.append((rid, speaker, party, gender, corpus, register))
        total += 1
        if len(batch) >= 10000:
            conn.executemany(
                "INSERT INTO speaker_sentences VALUES (?,?,?,?,?,?)", batch
            )
            conn.commit()
            elapsed = time.time() - t0
            print(f"  {total:,} rows inserted ({elapsed:.0f}s)")
            batch = []

    if batch:
        conn.executemany(
            "INSERT INTO speaker_sentences VALUES (?,?,?,?,?,?)", batch
        )
        conn.commit()

    elapsed = time.time() - t0
    print(f"\nDone. {total:,} speaker rows indexed, {skipped:,} skipped. [{elapsed:.1f}s]")

    # Show unique speakers
    cur = conn.execute(
        "SELECT speaker, COUNT(*) as cnt FROM speaker_sentences "
        "GROUP BY speaker ORDER BY cnt DESC LIMIT 20"
    )
    print("\nTop 20 speakers:")
    for name, cnt in cur:
        print(f"  {name}: {cnt:,}")

    conn.close()


if __name__ == "__main__":
    main()
