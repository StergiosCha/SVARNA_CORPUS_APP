#!/usr/bin/env python3
"""
Pre-compute discourse marker frequencies and store in a lookup table.
Run once after building the index. Makes the markers tab instant.

Usage:
    python3 precompute_markers.py corpus.db
"""
import json
import sqlite3
import sys
import time

DISCOURSE_MARKERS = {
    "causal": {
        "label_el": "Αιτιολογικοί",
        "markers": [
            "επειδή", "γιατί", "αφού", "διότι", "εφόσον",
            "καθώς", "λόγω", "εξαιτίας", "χάρη σε",
        ],
    },
    "sequential": {
        "label_el": "Χρονικής Διαδοχής",
        "markers": [
            "μετά", "έπειτα", "στη συνέχεια", "κατόπιν", "τελικά",
            "αρχικά", "πρώτα", "ύστερα", "εν συνεχεία", "προηγουμένως",
        ],
    },
    "additive": {
        "label_el": "Προσθετικοί",
        "markers": [
            "επίσης", "ακόμα", "ακόμη", "εξάλλου", "μάλιστα",
            "επιπλέον", "επιπροσθέτως", "παράλληλα", "συν τοις άλλοις",
        ],
    },
    "adversative": {
        "label_el": "Αντιθετικοί",
        "markers": [
            "αλλά", "όμως", "ωστόσο", "ενώ", "παρόλο",
            "μολονότι", "αντίθετα", "εντούτοις", "παρά ταύτα", "εν τούτοις",
        ],
    },
    "reformulative": {
        "label_el": "Αναδιατυπωτικοί",
        "markers": [
            "δηλαδή", "με άλλα λόγια", "ουσιαστικά",
            "πιο συγκεκριμένα", "ειδικότερα", "τουτέστιν", "ήτοι",
        ],
    },
    "conclusive": {
        "label_el": "Συμπερασματικοί",
        "markers": [
            "λοιπόν", "επομένως", "συνεπώς", "άρα",
            "κατά συνέπεια", "ως εκ τούτου", "συμπερασματικά", "τελικά",
        ],
    },
    "topic_management": {
        "label_el": "Διαχείρισης Θέματος",
        "markers": [
            "τέλος πάντων", "εν πάση περιπτώσει", "τέλος",
            "όσο αφορά", "σχετικά με", "αναφορικά με",
            "ως προς", "σε ό,τι αφορά",
        ],
    },
    "hedging": {
        "label_el": "Επιφυλακτικοί",
        "markers": [
            "βέβαια", "φυσικά", "ίσως", "θα έλεγα",
            "κατά κάποιον τρόπο", "τρόπον τινά", "ας πούμε",
            "κατά τη γνώμη μου",
        ],
    },
    "interactional": {
        "label_el": "Διαλογικοί/Προφορικοί",
        "markers": [
            "ε", "να", "ρε", "βρε", "πάμε",
            "κοίτα", "άκου", "ξέρεις", "καταλαβαίνεις", "δες",
        ],
    },
    "evidential": {
        "label_el": "Πηγής/Μαρτυρίας",
        "markers": [
            "φαίνεται", "λένε", "λέει", "δήθεν", "υποτίθεται",
            "σύμφωνα με", "κατά τα φαινόμενα", "εμφανώς",
        ],
    },
    "conditional": {
        "label_el": "Υποθετικοί",
        "markers": [
            "αν", "εάν", "σε περίπτωση που", "εκτός αν",
            "υπό τον όρο ότι", "αρκεί",
        ],
    },
}


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 precompute_markers.py corpus.db")
        sys.exit(1)

    db_path = sys.argv[1]
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size=-64000")

    # Create the lookup table
    conn.execute("DROP TABLE IF EXISTS marker_frequencies")
    conn.execute("""
        CREATE TABLE marker_frequencies (
            category TEXT,
            label_el TEXT,
            marker TEXT,
            register TEXT,
            count INTEGER,
            PRIMARY KEY (marker, register)
        )
    """)

    total_markers = sum(len(info["markers"]) for info in DISCOURSE_MARKERS.values())
    done = 0
    t0 = time.time()

    for category, info in DISCOURSE_MARKERS.items():
        label_el = info["label_el"]
        for marker in info["markers"]:
            done += 1
            # Quote multi-word markers for FTS5
            words = marker.split()
            if len(words) > 1:
                fts_q = f'text:"{marker}"'
            else:
                fts_q = f"text:{marker}"

            try:
                cursor = conn.execute(
                    "SELECT register, COUNT(*) as cnt "
                    "FROM sentences WHERE sentences MATCH ? "
                    "GROUP BY register",
                    [fts_q]
                )
                rows = cursor.fetchall()
            except Exception as e:
                print(f"  [WARN] {marker}: {e}")
                rows = []

            for reg, cnt in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO marker_frequencies "
                    "(category, label_el, marker, register, count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (category, label_el, marker, reg, cnt)
                )

            elapsed = time.time() - t0
            print(f"  [{done}/{total_markers}] {marker}: {len(rows)} registers [{elapsed:.0f}s]")

    conn.commit()

    # Verify
    cursor = conn.execute("SELECT COUNT(*) FROM marker_frequencies")
    total_rows = cursor.fetchone()[0]
    elapsed = time.time() - t0

    print()
    print(f"Done. {total_rows} rows in marker_frequencies table. [{elapsed:.0f}s]")
    conn.close()


if __name__ == "__main__":
    main()
