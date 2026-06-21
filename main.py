"""
Greek Corpus Workbench -- FastAPI backend
==========================================
Serves a KWIC concordancer, frequency comparisons, collocation analysis,
discourse-marker profiling, and corpus statistics from an FTS5-indexed
SQLite database of Greek text corpora.
"""

from __future__ import annotations

# Ensure SQLite has FTS5 support (pysqlite3-binary bundles it)
try:
    import pysqlite3 as _pysqlite3
    import sys
    sys.modules["sqlite3"] = _pysqlite3
except ImportError:
    pass  # Fall back to system sqlite3

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH: str = os.environ.get("DB_PATH", "/data/corpus.db")
APP_DIR: str = os.path.dirname(os.path.abspath(__file__))

# Multi-database support: map logical names to file paths.
# DB_PATH is always the default ("corpus"). Additional databases are
# discovered from LITERATURE_DB_PATH and DIALECTAL_DB_PATH env vars.
DB_PATHS: dict[str, str] = {"corpus": DB_PATH}
if os.environ.get("LITERATURE_DB_PATH"):
    DB_PATHS["literature"] = os.environ["LITERATURE_DB_PATH"]
if os.environ.get("DIALECTAL_DB_PATH"):
    DB_PATHS["dialectal"] = os.environ["DIALECTAL_DB_PATH"]

app = FastAPI(
    title="Greek Corpus Workbench",
    version="1.0.0",
    description="API for querying and analysing Greek text corpora.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Greek stopwords (50+ high-frequency function words)
# ---------------------------------------------------------------------------

GREEK_STOPWORDS: set[str] = {
    "ο", "η", "το", "τα", "τον", "την", "του", "της", "των", "τους", "τις",
    "οι", "στο", "στη", "στα", "στον", "στην", "στους", "στις", "στων",
    "και", "να", "με", "σε", "για", "από", "ως", "ότι", "πως", "που",
    "αν", "θα", "δεν", "μην", "δε", "μη",
    "είναι", "ήταν", "είμαι", "έχει", "έχω", "έχουν",
    "ένα", "ένας", "μία", "μια",
    "αυτό", "αυτός", "αυτή", "αυτά", "αυτοί", "αυτές",
    "αυτού", "αυτής", "αυτών", "αυτόν", "αυτήν",
    "εγώ", "εσύ", "εμείς", "εσείς", "αυτοί",
    "μου", "σου", "μας", "σας", "τους",
    "κάτι", "κάποιος", "κάποια", "κάποιο",
    "πού", "πώς", "πότε", "ποιος", "ποια", "ποιο",
    "πολύ", "πιο", "κι", "κ", "ή",
    "όλα", "όλο", "όλοι", "όλες", "όλη",
    "εδώ", "εκεί", "τώρα", "μόνο",
    "πριν", "μετά", "πάνω", "κάτω",
    "ίδιο", "ίδια", "ίδιος",
}

# Corpus artifacts / web junk that leak into text from scraping pipelines
JUNK_TOKENS: set[str] = {
    "sw", "br", "gt", "lt", "amp", "nbsp", "ref", "http", "https", "www",
    "com", "org", "html", "php", "jpg", "png", "pdf", "url", "xml",
    "id", "div", "css", "js", "src", "img", "href", "td", "tr", "th",
    "ul", "li", "ol", "dl", "dt", "dd", "px", "em", "pt",
}


def _is_junk(token: str) -> bool:
    """Return True for corpus artifacts (not real words)."""
    return token in JUNK_TOKENS


def _strip_accents(text: str) -> str:
    """Remove combining diacritical marks (accents) from Greek text."""
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------------------
# Discourse-marker lexicon
# ---------------------------------------------------------------------------

DISCOURSE_MARKERS: dict[str, dict[str, Any]] = {
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

# Reverse lookup: marker text -> category
_MARKER_TO_CATEGORY: dict[str, str] = {}
for _cat, _info in DISCOURSE_MARKERS.items():
    for _m in _info["markers"]:
        _MARKER_TO_CATEGORY[_m] = _cat

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FTS5_SPECIAL = re.compile(r'["\*\(\)\+\-\^]')
_WORD_RE = re.compile(r"[\wͰ-Ͽἀ-῿]+", re.UNICODE)


def _sanitise_fts_query(raw: str) -> str:
    """Escape FTS5 special characters and wrap multi-word phrases in quotes."""
    q = raw.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty query")
    # Escape characters that have meaning in FTS5 syntax
    q = _FTS5_SPECIAL.sub(lambda m: "\\" + m.group(), q)
    # Multi-word queries become a phrase query
    words = q.split()
    if len(words) > 1:
        q = '"' + " ".join(words) + '"'
    return q


def _build_fts_where(
    fts_query: str,
    corpus: str | None = None,
    register: str | None = None,
    mode: str | None = None,
) -> tuple[str, list[Any]]:
    """Build an FTS5 MATCH expression with optional column filters.

    Returns (match_expression, params) where params is a list of bind values.
    The column filters are embedded inside the MATCH string using FTS5
    column-filter syntax so that the query planner can use the index.
    """
    parts: list[str] = []
    if corpus:
        parts.append(f"corpus:{_sanitise_fts_query(corpus)}")
    if register:
        parts.append(f"register:{_sanitise_fts_query(register)}")
    if mode:
        parts.append(f"mode:{_sanitise_fts_query(mode)}")
    parts.append(f"text:{fts_query}")
    match_expr = " ".join(parts)
    return match_expr, [match_expr]


def _extract_kwic(
    text: str,
    query: str,
    left_n: int = 8,
    right_n: int = 8,
) -> tuple[str, str, str]:
    """Extract KWIC (KeyWord In Context) from *text* for *query*.

    Returns (left_context, keyword, right_context).
    """
    words = text.split()
    query_words = query.strip().split()
    query_len = len(query_words)

    # Try case-insensitive matching for the (possibly multi-word) query.
    query_lower = [w.lower() for w in query_words]
    words_lower = [w.lower() for w in words]

    match_idx: int | None = None
    for i in range(len(words_lower) - query_len + 1):
        # Strip punctuation from edges for comparison
        candidate = [
            re.sub(r"^\W+|\W+$", "", w) for w in words_lower[i : i + query_len]
        ]
        if candidate == [re.sub(r"^\W+|\W+$", "", w) for w in query_lower]:
            match_idx = i
            break

    if match_idx is None:
        # Fallback: try to find any single query word
        for qw in query_lower:
            qw_clean = re.sub(r"^\W+|\W+$", "", qw)
            for i, wl in enumerate(words_lower):
                if re.sub(r"^\W+|\W+$", "", wl) == qw_clean:
                    match_idx = i
                    query_len = 1
                    break
            if match_idx is not None:
                break

    if match_idx is None:
        # Cannot locate keyword -- return whole text trimmed
        mid = len(words) // 2
        left = " ".join(words[max(0, mid - left_n) : mid])
        kw = words[mid] if words else ""
        right = " ".join(words[mid + 1 : mid + 1 + right_n])
        return left, kw, right

    left_start = max(0, match_idx - left_n)
    right_end = min(len(words), match_idx + query_len + right_n)
    left_ctx = " ".join(words[left_start:match_idx])
    keyword = " ".join(words[match_idx : match_idx + query_len])
    right_ctx = " ".join(words[match_idx + query_len : right_end])
    return left_ctx, keyword, right_ctx


def _tokenise(text: str) -> list[str]:
    """Split Greek text into lowercased word tokens."""
    return [m.group().lower() for m in _WORD_RE.finditer(text)]


def _is_punctuation_or_number(token: str) -> bool:
    return all(
        unicodedata.category(ch).startswith(("P", "S", "N", "Z"))
        for ch in token
    )


def _resolve_db_path(db_name: str | None = None) -> str:
    """Return the file path for the given database name."""
    if not db_name or db_name not in DB_PATHS:
        return DB_PATH  # default
    return DB_PATHS[db_name]


async def _get_db(db_name: str | None = None) -> aiosqlite.Connection:
    path = _resolve_db_path(db_name)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    return db


async def _total_sentences(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COALESCE(SUM(sentence_count), 0) FROM corpus_stats"
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def _total_tokens(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COALESCE(SUM(token_count), 0) FROM corpus_stats"
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


def _positional_bucket(
    text: str, query: str
) -> str:
    """Determine whether *query* appears sentence-initial, medial, or final."""
    words = text.split()
    query_lower = query.lower().split()
    words_lower = [w.lower() for w in words]

    match_idx: int | None = None
    qlen = len(query_lower)
    for i in range(len(words_lower) - qlen + 1):
        candidate = [re.sub(r"^\W+|\W+$", "", w) for w in words_lower[i : i + qlen]]
        target = [re.sub(r"^\W+|\W+$", "", w) for w in query_lower]
        if candidate == target:
            match_idx = i
            break

    if match_idx is None:
        return "medial"
    if match_idx <= 1:
        return "initial"
    if match_idx + qlen >= len(words) - 1:
        return "final"
    return "medial"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _verify_database() -> None:
    import logging
    logger = logging.getLogger("uvicorn")
    logger.info(f"Checking database at {DB_PATH} ...")
    # Wait up to 60s for the file to appear (Azure File Share can be slow)
    import asyncio
    for attempt in range(12):
        if os.path.isfile(DB_PATH):
            break
        logger.warning(f"Database not found yet, retrying ({attempt+1}/12)...")
        await asyncio.sleep(5)
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(
            f"Database not found at {DB_PATH}. "
            "Set the DB_PATH environment variable to the correct path."
        )
    logger.info("Database file found, verifying...")
    # Quick sanity check (skip if it takes too long over SMB)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sentences'"
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    logger.warning("sentences table not found, but continuing anyway")
                else:
                    logger.info("Database verified OK")
    except Exception as exc:
        logger.warning(f"Database verification query failed: {exc} -- continuing anyway")


# ---------------------------------------------------------------------------
# GET /api/databases -- list available databases
# ---------------------------------------------------------------------------

@app.get("/api/databases")
async def list_databases() -> JSONResponse:
    """List available databases with basic info."""
    result = []
    for name, path in DB_PATHS.items():
        entry: dict[str, Any] = {"name": name, "available": os.path.isfile(path)}
        if os.path.isfile(path):
            try:
                async with aiosqlite.connect(path) as db:
                    async with db.execute(
                        "SELECT COALESCE(SUM(sentence_count),0), "
                        "COALESCE(SUM(token_count),0) FROM corpus_stats"
                    ) as cur:
                        row = await cur.fetchone()
                        entry["sentences"] = row[0] if row else 0
                        entry["tokens"] = row[1] if row else 0
            except Exception:
                entry["sentences"] = 0
                entry["tokens"] = 0
        result.append(entry)
    return JSONResponse({"databases": result})


# ---------------------------------------------------------------------------
# GET / -- serve frontend
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def serve_frontend() -> FileResponse:
    index_path = os.path.join(APP_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(
        index_path,
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/search -- KWIC concordancer
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search(
    q: str = Query("", description="Search query (optional if speaker is set)"),
    corpus: str | None = Query(None, description="Filter by corpus name"),
    register: str | None = Query(None, description="Filter by register tag"),
    mode: str | None = Query(None, description="Filter by mode"),
    speaker: str | None = Query(None, description="Filter by speaker name (parliament metadata)"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    # Speaker-only search: use precomputed speaker_sentences table (fast)
    if speaker and not q:
        # Normalize: strip accents, lowercase (DB stores names this way)
        speaker_norm = _strip_accents(speaker).lower()
        db = await _get_db(db_name)
        try:
            # Check if precomputed table exists
            has_table = False
            try:
                async with db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='speaker_sentences'"
                ) as cur:
                    has_table = (await cur.fetchone()) is not None
            except Exception:
                pass

            if has_table:
                # Fast path: indexed speaker column
                like_pat = f"%{speaker_norm}%"
                count_sql = "SELECT COUNT(*) FROM speaker_sentences WHERE speaker LIKE ?"
                async with db.execute(count_sql, [like_pat]) as cur:
                    total = (await cur.fetchone())[0]
                sql = (
                    "SELECT s.text, s.corpus, s.register, s.mode, s.year, s.metadata "
                    "FROM speaker_sentences sp "
                    "JOIN sentences s ON s.rowid = sp.rowid "
                    "WHERE sp.speaker LIKE ? "
                    "LIMIT ? OFFSET ?"
                )
                async with db.execute(sql, [like_pat, limit, offset]) as cur:
                    rows = await cur.fetchall()
            else:
                # Slow fallback: full table scan on metadata LIKE
                like_pat = f"%{speaker_norm}%"
                count_sql = "SELECT COUNT(*) FROM sentences WHERE metadata LIKE ?"
                async with db.execute(count_sql, [like_pat]) as cur:
                    total = (await cur.fetchone())[0]
                sql = (
                    "SELECT text, corpus, register, mode, year, metadata "
                    "FROM sentences WHERE metadata LIKE ? LIMIT ? OFFSET ?"
                )
                async with db.execute(sql, [like_pat, limit, offset]) as cur:
                    rows = await cur.fetchall()

            results = []
            for row in rows:
                text = row[0]
                results.append({
                    "left_context": "", "keyword": "", "right_context": "",
                    "full_text": text, "corpus": row[1], "register": row[2],
                    "mode": row[3], "year": row[4], "metadata": row[5],
                })
            return JSONResponse({"results": results, "total": total, "query": f"speaker:{speaker}"})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            await db.close()

    if not q:
        raise HTTPException(status_code=400, detail="Query or speaker required")

    fts_q = _sanitise_fts_query(q)
    match_expr, params = _build_fts_where(fts_q, corpus, register, mode)

    db = await _get_db(db_name)
    try:
        # Total count
        count_sql = (
            "SELECT COUNT(*) FROM sentences WHERE sentences MATCH ?"
        )
        async with db.execute(count_sql, params) as cur:
            total = (await cur.fetchone())[0]

        # Fetch rows
        fetch_sql = (
            "SELECT text, corpus, register, mode, year, metadata "
            "FROM sentences WHERE sentences MATCH ? "
            "ORDER BY rank LIMIT ? OFFSET ?"
        )
        async with db.execute(fetch_sql, params + [limit, offset]) as cur:
            rows = await cur.fetchall()

        results = []
        for row in rows:
            text = row[0]
            meta = row[5] or ""
            # If speaker filter is set, skip non-matching rows
            if speaker and speaker.lower() not in meta.lower():
                continue
            left_ctx, keyword, right_ctx = _extract_kwic(text, q)
            results.append(
                {
                    "left_context": left_ctx,
                    "keyword": keyword,
                    "right_context": right_ctx,
                    "full_text": text,
                    "corpus": row[1],
                    "register": row[2],
                    "mode": row[3],
                    "year": row[4],
                    "metadata": row[5],
                }
            )

        return JSONResponse(
            {"results": results, "total": total, "query": q}
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/frequency -- Frequency comparison across registers
# ---------------------------------------------------------------------------

@app.get("/api/frequency")
async def frequency(
    q: str = Query(..., min_length=1, description="Search query"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    fts_q = _sanitise_fts_query(q)

    db = await _get_db(db_name)
    try:
        # Count per corpus/register
        count_sql = (
            "SELECT corpus, register, mode, COUNT(*) as cnt "
            "FROM sentences WHERE sentences MATCH ? "
            "GROUP BY corpus, register, mode"
        )
        # We need the match to be on text column only
        text_match = f"text:{fts_q}"
        async with db.execute(count_sql, [text_match]) as cur:
            freq_rows = await cur.fetchall()

        # Corpus stats
        async with db.execute(
            "SELECT corpus, register, mode, sentence_count, token_count "
            "FROM corpus_stats"
        ) as cur:
            stats_rows = await cur.fetchall()

        stats_map: dict[tuple[str, str, str], dict[str, int]] = {}
        for sr in stats_rows:
            key = (sr[0], sr[1], sr[2])
            stats_map[key] = {
                "sentence_count": sr[3] or 0,
                "token_count": sr[4] or 0,
            }

        frequencies = []
        for fr in freq_rows:
            corpus_name = fr[0]
            reg = fr[1]
            m = fr[2]
            raw_count = fr[3]
            key = (corpus_name, reg, m)
            stats = stats_map.get(key, {"sentence_count": 0, "token_count": 0})
            total_tokens = stats["token_count"]
            per_mil = (
                (raw_count / total_tokens) * 1_000_000
                if total_tokens > 0
                else 0.0
            )
            frequencies.append(
                {
                    "corpus": corpus_name,
                    "register": reg,
                    "mode": m,
                    "raw_count": raw_count,
                    "total_sentences": stats["sentence_count"],
                    "total_tokens": total_tokens,
                    "per_million": round(per_mil, 2),
                }
            )

        return JSONResponse({"query": q, "frequencies": frequencies})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/collocations -- Words that appear near the query term
# ---------------------------------------------------------------------------

@app.get("/api/collocations")
async def collocations(
    q: str = Query(..., min_length=1, description="Search query"),
    window: int = Query(5, ge=1, le=15, description="Context window size"),
    limit: int = Query(30, ge=1, le=200, description="Max collocates"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    fts_q = _sanitise_fts_query(q)
    text_match = f"text:{fts_q}"

    db = await _get_db(db_name)
    try:
        # Fetch matching sentences (cap to 2000 for performance on SMB-mounted DBs)
        fetch_sql = (
            "SELECT text FROM sentences WHERE sentences MATCH ? "
            "ORDER BY rank LIMIT 2000"
        )
        async with db.execute(fetch_sql, [text_match]) as cur:
            rows = await cur.fetchall()

        query_tokens = set(_tokenise(q))
        cooccurrence: Counter[str] = Counter()
        collocate_total: Counter[str] = Counter()
        query_freq = len(rows)  # number of sentences containing query
        total_n = await _total_sentences(db)
        if total_n == 0:
            total_n = max(query_freq, 1)

        for row in rows:
            text = row[0]
            tokens = _tokenise(text)
            # Find query position(s)
            q_lower = [t.lower() for t in q.strip().split()]
            q_len = len(q_lower)
            match_positions: list[int] = []
            for i in range(len(tokens) - q_len + 1):
                if tokens[i : i + q_len] == [
                    re.sub(r"^\W+|\W+$", "", w) for w in q_lower
                ]:
                    match_positions.append(i)

            if not match_positions:
                # Fallback: single word match
                for i, tok in enumerate(tokens):
                    if tok in query_tokens:
                        match_positions.append(i)

            # Collect collocates
            seen_in_sentence: set[str] = set()
            for pos in match_positions:
                start = max(0, pos - window)
                end = min(len(tokens), pos + q_len + window)
                for j in range(start, end):
                    if j >= pos and j < pos + q_len:
                        continue
                    tok = tokens[j]
                    if (
                        tok in query_tokens
                        or tok in GREEK_STOPWORDS
                        or _is_punctuation_or_number(tok)
                        or len(tok) < 2
                    ):
                        continue
                    cooccurrence[tok] += 1
                    if tok not in seen_in_sentence:
                        seen_in_sentence.add(tok)

            # Also count all tokens in these sentences for f(y) estimation
            for tok in set(tokens):
                if (
                    tok not in query_tokens
                    and tok not in GREEK_STOPWORDS
                    and not _is_punctuation_or_number(tok)
                    and len(tok) >= 2
                ):
                    collocate_total[tok] += 1

        # Compute MI scores
        results = []
        for word, fxy in cooccurrence.most_common(limit * 3):
            fy = collocate_total.get(word, 1)
            # MI = log2( f(x,y) * N / (f(x) * f(y)) )
            mi = math.log2((fxy * total_n) / (query_freq * fy)) if fy > 0 else 0.0
            if fxy >= 2:  # require minimum co-occurrence
                results.append(
                    {
                        "word": word,
                        "frequency": fxy,
                        "mi_score": round(mi, 3),
                    }
                )

        # Sort by MI descending, take top N
        results.sort(key=lambda x: x["mi_score"], reverse=True)
        results = results[:limit]

        return JSONResponse({"query": q, "collocations": results})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/markers -- Discourse marker analysis
# ---------------------------------------------------------------------------

@app.get("/api/markers")
async def markers(
    marker: str = Query("all", description='Specific marker or "all"'),
    corpus: str | None = Query(None, description="Filter by corpus"),
    register: str | None = Query(None, description="Filter by register"),
    examples: int = Query(10, ge=5, le=200, description="Number of examples to return"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    db = await _get_db(db_name)
    try:
        if marker == "all":
            return await _markers_overview(db, corpus, register)
        else:
            return await _marker_detail(db, marker, corpus, register, examples)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


async def _markers_overview(
    db: aiosqlite.Connection,
    corpus: str | None,
    register: str | None,
) -> JSONResponse:
    """Return the full lexicon with per-register frequency counts.

    Uses the pre-computed marker_frequencies table for instant results.
    Falls back to live FTS5 queries if the table doesn't exist.
    """
    # Try pre-computed table first (instant)
    try:
        sql = "SELECT category, label_el, marker, register, count FROM marker_frequencies"
        params: list[Any] = []
        if register:
            sql += " WHERE register = ?"
            params.append(register)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

        if rows:
            # Build intermediate dict: cat -> marker -> {register: count}
            raw: dict[str, Any] = {}
            for row in rows:
                cat, label_el, mrk, reg, cnt = row[0], row[1], row[2], row[3], row[4]
                if cat not in raw:
                    raw[cat] = {"label_el": label_el, "markers_dict": {}}
                if mrk not in raw[cat]["markers_dict"]:
                    raw[cat]["markers_dict"][mrk] = {}
                raw[cat]["markers_dict"][mrk][reg] = cnt

            # Convert to array format the frontend expects
            result: dict[str, Any] = {}
            for cat, info2 in raw.items():
                marker_list = []
                for mrk, reg_counts in info2["markers_dict"].items():
                    total = sum(reg_counts.values())
                    marker_list.append({
                        "marker": mrk,
                        "total_count": total,
                        "by_register": reg_counts,
                    })
                marker_list.sort(key=lambda x: x["total_count"], reverse=True)
                result[cat] = {
                    "label_el": info2["label_el"],
                    "markers": marker_list,
                }
            return JSONResponse({"lexicon": result})
    except Exception:
        pass  # Table doesn't exist, fall back to live queries

    # Fallback: live FTS5 queries (slow on large DBs)
    result = {}
    for category, info in DISCOURSE_MARKERS.items():
        marker_list = []
        for m in info["markers"]:
            fts_q = _sanitise_fts_query(m)
            match_expr, params2 = _build_fts_where(fts_q, corpus, register)
            count_sql = (
                "SELECT register, COUNT(*) as cnt "
                "FROM sentences WHERE sentences MATCH ? "
                "GROUP BY register"
            )
            async with db.execute(count_sql, params2) as cur:
                mrows = await cur.fetchall()
            freq_by_register: dict[str, int] = {}
            for row in mrows:
                freq_by_register[row[0]] = row[1]
            total = sum(freq_by_register.values())
            marker_list.append({
                "marker": m,
                "total_count": total,
                "by_register": freq_by_register,
            })
        marker_list.sort(key=lambda x: x["total_count"], reverse=True)
        result[category] = {
            "label_el": info["label_el"],
            "markers": marker_list,
        }

    return JSONResponse({"lexicon": result})


async def _marker_detail(
    db: aiosqlite.Connection,
    marker: str,
    corpus: str | None,
    register: str | None,
    max_examples: int = 10,
) -> JSONResponse:
    """Detailed analysis for a single discourse marker."""
    category = _MARKER_TO_CATEGORY.get(marker)

    # Frequency per register: use pre-computed table (instant)
    frequencies_by_register: dict[str, int] = {}
    try:
        freq_sql = "SELECT register, count FROM marker_frequencies WHERE marker = ?"
        freq_params: list[Any] = [marker]
        if register:
            freq_sql += " AND register = ?"
            freq_params.append(register)
        async with db.execute(freq_sql, freq_params) as cur:
            for row in await cur.fetchall():
                frequencies_by_register[row[0]] = row[1]
    except Exception:
        pass  # table missing, leave empty

    fts_q = _sanitise_fts_query(marker)
    match_expr, params = _build_fts_where(fts_q, corpus, register)

    # Fetch sentences for positional analysis + examples (no ranking for speed)
    fetch_limit = max(max_examples, 50)
    fetch_sql = (
        "SELECT text, corpus, register, mode, year, metadata "
        "FROM sentences WHERE sentences MATCH ? "
        f"LIMIT {fetch_limit}"
    )
    async with db.execute(fetch_sql, params) as cur:
        rows = await cur.fetchall()

    # Positional analysis
    position_counts: Counter[str] = Counter()
    for row in rows:
        bucket = _positional_bucket(row[0], marker)
        position_counts[bucket] += 1

    total_pos = sum(position_counts.values()) or 1
    position = {
        "initial": round(position_counts["initial"] / total_pos * 100, 1),
        "medial": round(position_counts["medial"] / total_pos * 100, 1),
        "final": round(position_counts["final"] / total_pos * 100, 1),
    }

    # Example concordance lines
    examples = []
    for row in rows[:max_examples]:
        text = row[0]
        left_ctx, keyword, right_ctx = _extract_kwic(text, marker)
        examples.append(
            {
                "left_context": left_ctx,
                "keyword": keyword,
                "right_context": right_ctx,
                "full_text": text,
                "corpus": row[1],
                "register": row[2],
                "mode": row[3],
                "year": row[4],
                "metadata": row[5],
            }
        )

    return JSONResponse(
        {
            "marker": marker,
            "category": category,
            "frequencies_by_register": frequencies_by_register,
            "position": position,
            "examples": examples,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/stats -- Corpus statistics
# ---------------------------------------------------------------------------

@app.get("/api/debug")
async def debug_info(
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    import sqlite3 as _sq3
    info: dict[str, Any] = {
        "sqlite_version": _sq3.sqlite_version,
        "sqlite_module": _sq3.__file__ if hasattr(_sq3, "__file__") else str(type(_sq3)),
        "db_path": DB_PATH,
        "db_exists": os.path.isfile(DB_PATH),
    }
    try:
        db = await _get_db(db_name)
        try:
            # List all tables
            async with db.execute("SELECT name, type FROM sqlite_master") as cur:
                info["tables"] = [{"name": r[0], "type": r[1]} for r in await cur.fetchall()]
            # Test FTS5
            try:
                async with db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5test USING fts5(x)") as cur:
                    pass
                info["fts5_available"] = True
            except Exception as e:
                info["fts5_available"] = False
                info["fts5_error"] = str(e)
            # Test the actual sentences table
            try:
                async with db.execute("SELECT COUNT(*) FROM sentences LIMIT 1") as cur:
                    info["sentences_count"] = (await cur.fetchone())[0]
            except Exception as e:
                info["sentences_error"] = str(e)
        finally:
            await db.close()
    except Exception as e:
        info["db_error"] = str(e)
    return JSONResponse(info)


@app.get("/api/stats")
async def stats(
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    db = await _get_db(db_name)
    try:
        async with db.execute(
            "SELECT corpus, register, mode, sentence_count, token_count "
            "FROM corpus_stats"
        ) as cur:
            rows = await cur.fetchall()

        data = [
            {
                "corpus": row[0],
                "register": row[1],
                "mode": row[2],
                "sentence_count": row[3],
                "token_count": row[4],
            }
            for row in rows
        ]
        return JSONResponse({"stats": data})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/speakers -- List unique speakers from parliament metadata
# ---------------------------------------------------------------------------

@app.get("/api/speakers")
async def speakers(
    q: str | None = Query(None, description="Filter speakers by name substring"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    db = await _get_db(db_name)
    try:
        # Try precomputed table first
        has_table = False
        try:
            async with db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='speaker_sentences'"
            ) as cur:
                has_table = (await cur.fetchone()) is not None
        except Exception:
            pass

        if has_table:
            if q:
                sql = (
                    "SELECT speaker, party, gender, COUNT(*) as cnt "
                    "FROM speaker_sentences WHERE speaker LIKE ? "
                    "GROUP BY speaker, party, gender ORDER BY cnt DESC"
                )
                async with db.execute(sql, [f"%{q}%"]) as cur:
                    rows = await cur.fetchall()
            else:
                sql = (
                    "SELECT speaker, party, gender, COUNT(*) as cnt "
                    "FROM speaker_sentences "
                    "GROUP BY speaker, party, gender ORDER BY cnt DESC"
                )
                async with db.execute(sql) as cur:
                    rows = await cur.fetchall()
            result = [
                {"speaker": r[0], "party": r[1], "gender": r[2], "count": r[3]}
                for r in rows
            ]
            return JSONResponse({"speakers": result, "total": len(result)})

        # Slow fallback: scan metadata
        sql = (
            "SELECT DISTINCT metadata FROM sentences "
            "WHERE metadata IS NOT NULL AND metadata != '' "
            "AND metadata LIKE '%speaker%'"
        )
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()

        seen: dict[str, dict] = {}
        for (meta_str,) in rows:
            try:
                meta = json.loads(meta_str)
            except (json.JSONDecodeError, TypeError):
                continue
            name = meta.get("speaker", "").strip()
            if not name:
                continue
            if q and q.lower() not in name.lower():
                continue
            if name not in seen:
                seen[name] = {
                    "speaker": name,
                    "party": meta.get("party", ""),
                    "gender": meta.get("gender", ""),
                }

        result = sorted(seen.values(), key=lambda x: x["speaker"])
        return JSONResponse({"speakers": result, "total": len(result)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/compare -- Cross-register comparison
# ---------------------------------------------------------------------------

@app.get("/api/compare")
async def compare(
    q: str = Query(..., min_length=1, description="Search query"),
    registers: str = Query(
        ..., min_length=1,
        description="Comma-separated register list",
    ),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    register_list = [r.strip() for r in registers.split(",") if r.strip()]
    if not register_list:
        raise HTTPException(status_code=400, detail="No registers specified")

    fts_q = _sanitise_fts_query(q)

    db = await _get_db(db_name)
    try:
        # Corpus stats lookup
        async with db.execute(
            "SELECT corpus, register, mode, sentence_count, token_count "
            "FROM corpus_stats"
        ) as cur:
            stats_rows = await cur.fetchall()

        # Aggregate token counts per register
        tokens_per_register: dict[str, int] = {}
        for sr in stats_rows:
            reg = sr[1]
            tokens_per_register[reg] = (
                tokens_per_register.get(reg, 0) + (sr[4] or 0)
            )

        total_n = await _total_sentences(db)

        results = []
        for reg in register_list:
            match_expr = f"register:{_sanitise_fts_query(reg)} text:{fts_q}"

            # Frequency
            async with db.execute(
                "SELECT COUNT(*) FROM sentences WHERE sentences MATCH ?",
                [match_expr],
            ) as cur:
                freq = (await cur.fetchone())[0]

            total_tok = tokens_per_register.get(reg, 0)
            per_mil = (freq / total_tok) * 1_000_000 if total_tok > 0 else 0.0

            # Top collocates (from up to 2000 sentences)
            fetch_sql = (
                "SELECT text FROM sentences WHERE sentences MATCH ? "
                "ORDER BY rank LIMIT 2000"
            )
            async with db.execute(fetch_sql, [match_expr]) as cur:
                sent_rows = await cur.fetchall()

            query_tokens = set(_tokenise(q))
            colloc_counter: Counter[str] = Counter()
            q_lower = [t.lower() for t in q.strip().split()]
            q_len = len(q_lower)

            for sr in sent_rows:
                tokens = _tokenise(sr[0])
                match_positions: list[int] = []
                for i in range(len(tokens) - q_len + 1):
                    if tokens[i : i + q_len] == [
                        re.sub(r"^\W+|\W+$", "", w) for w in q_lower
                    ]:
                        match_positions.append(i)
                if not match_positions:
                    for i, tok in enumerate(tokens):
                        if tok in query_tokens:
                            match_positions.append(i)
                for pos in match_positions:
                    start = max(0, pos - 5)
                    end = min(len(tokens), pos + q_len + 5)
                    for j in range(start, end):
                        if j >= pos and j < pos + q_len:
                            continue
                        tok = tokens[j]
                        if (
                            tok in query_tokens
                            or tok in GREEK_STOPWORDS
                            or _is_punctuation_or_number(tok)
                            or len(tok) < 2
                        ):
                            continue
                        colloc_counter[tok] += 1

            top_collocates = [
                {"word": w, "count": c}
                for w, c in colloc_counter.most_common(10)
            ]

            # Positional distribution
            pos_counts: Counter[str] = Counter()
            for sr in sent_rows:
                bucket = _positional_bucket(sr[0], q)
                pos_counts[bucket] += 1
            total_pos = sum(pos_counts.values()) or 1
            position = {
                "initial": round(pos_counts["initial"] / total_pos * 100, 1),
                "medial": round(pos_counts["medial"] / total_pos * 100, 1),
                "final": round(pos_counts["final"] / total_pos * 100, 1),
            }

            results.append(
                {
                    "register": reg,
                    "frequency": freq,
                    "per_million": round(per_mil, 2),
                    "collocates": top_collocates,
                    "position": position,
                }
            )

        return JSONResponse({"query": q, "registers": results})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/regex -- Regex search (pattern matching on raw text)
# ---------------------------------------------------------------------------

@app.get("/api/regex")
async def regex_search(
    pattern: str = Query(..., min_length=1, description="Python regex pattern"),
    corpus: str | None = Query(None),
    register: str | None = Query(None),
    mode: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Search with Python regex against raw sentence text."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE | re.UNICODE)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")

    db = await _get_db(db_name)
    try:
        # Build filter SQL (non-FTS, regular table scan via content table)
        where_parts: list[str] = []
        params: list[Any] = []
        if corpus:
            where_parts.append("corpus = ?")
            params.append(corpus)
        if register:
            where_parts.append("register = ?")
            params.append(register)
        if mode:
            where_parts.append("mode = ?")
            params.append(mode)

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # We scan the content table and apply regex in Python
        # FTS5 content table is named sentences_content for an fts5 table named sentences
        # But we can also query the FTS5 table directly and filter in Python
        sql = f"SELECT text, corpus, register, mode, year, metadata FROM sentences{where_clause}"
        results = []
        total = 0

        async with db.execute(sql, params) as cur:
            async for row in cur:
                text = row[0]
                if compiled.search(text):
                    total += 1
                    if total > offset and len(results) < limit:
                        match = compiled.search(text)
                        results.append({
                            "full_text": text,
                            "match": match.group() if match else "",
                            "corpus": row[1],
                            "register": row[2],
                            "mode": row[3],
                            "year": row[4],
                            "metadata": row[5],
                        })
                    # Stop scanning after enough results for performance
                    if total >= offset + limit + 10000:
                        break

        return JSONResponse({"results": results, "total": total, "pattern": pattern})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/wordfreq -- Word frequency list for a corpus/register
# ---------------------------------------------------------------------------

@app.get("/api/wordfreq")
async def word_frequency(
    corpus: str | None = Query(None),
    register: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    sample: int = Query(50000, ge=1000, le=500000, description="Sentences to sample"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Top word frequencies for a corpus or register slice."""
    db = await _get_db(db_name)
    try:
        where_parts: list[str] = []
        params: list[Any] = []
        if corpus:
            where_parts.append("corpus = ?")
            params.append(corpus)
        if register:
            where_parts.append("register = ?")
            params.append(register)

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = f"SELECT text FROM sentences{where_clause} LIMIT ?"
        params.append(sample)

        word_counts: Counter[str] = Counter()
        total_tokens = 0

        async with db.execute(sql, params) as cur:
            async for row in cur:
                tokens = _tokenise(row[0])
                for tok in tokens:
                    if tok not in GREEK_STOPWORDS and not _is_punctuation_or_number(tok) and not _is_junk(tok) and len(tok) >= 2:
                        word_counts[tok] += 1
                        total_tokens += 1

        results = [
            {"rank": i + 1, "word": w, "count": c, "per_million": round((c / total_tokens) * 1_000_000, 1) if total_tokens > 0 else 0}
            for i, (w, c) in enumerate(word_counts.most_common(limit))
        ]

        return JSONResponse({
            "words": results,
            "total_tokens": total_tokens,
            "unique_types": len(word_counts),
            "ttr": round(len(word_counts) / total_tokens, 4) if total_tokens > 0 else 0,
            "corpus": corpus,
            "register": register,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/ngrams -- N-gram frequency
# ---------------------------------------------------------------------------

@app.get("/api/ngrams")
async def ngrams(
    n: int = Query(2, ge=2, le=4, description="N-gram size"),
    corpus: str | None = Query(None),
    register: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    sample: int = Query(30000, ge=1000, le=200000),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Top n-grams for a corpus or register slice."""
    db = await _get_db(db_name)
    try:
        where_parts: list[str] = []
        params: list[Any] = []
        if corpus:
            where_parts.append("corpus = ?")
            params.append(corpus)
        if register:
            where_parts.append("register = ?")
            params.append(register)

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = f"SELECT text FROM sentences{where_clause} LIMIT ?"
        params.append(sample)

        ngram_counts: Counter[str] = Counter()

        async with db.execute(sql, params) as cur:
            async for row in cur:
                tokens = _tokenise(row[0])
                # Filter stopwords for cleaner n-grams
                filtered = [t for t in tokens if t not in GREEK_STOPWORDS and not _is_junk(t) and len(t) >= 2]
                for i in range(len(filtered) - n + 1):
                    gram = " ".join(filtered[i:i + n])
                    ngram_counts[gram] += 1

        results = [
            {"ngram": gram, "count": c, "rank": i + 1}
            for i, (gram, c) in enumerate(ngram_counts.most_common(limit))
            if c >= 2
        ]

        return JSONResponse({"ngrams": results, "n": n, "corpus": corpus, "register": register})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/keywords -- Keyword analysis (log-likelihood) between registers
# ---------------------------------------------------------------------------

@app.get("/api/keywords")
async def keywords(
    target: str = Query(..., description="Target register"),
    reference: str = Query(..., description="Reference register"),
    limit: int = Query(50, ge=1, le=200),
    sample: int = Query(30000, ge=1000, le=200000),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Keywords statistically overrepresented in target vs reference register."""
    db = await _get_db(db_name)
    try:
        async def count_words(register: str) -> tuple[Counter, int]:
            sql = "SELECT text FROM sentences WHERE register = ? LIMIT ?"
            counts: Counter[str] = Counter()
            total = 0
            async with db.execute(sql, [register, sample]) as cur:
                async for row in cur:
                    tokens = _tokenise(row[0])
                    for tok in tokens:
                        if tok not in GREEK_STOPWORDS and not _is_punctuation_or_number(tok) and not _is_junk(tok) and len(tok) >= 2:
                            counts[tok] += 1
                            total += 1
            return counts, total

        target_counts, target_total = await count_words(target)
        ref_counts, ref_total = await count_words(reference)

        if target_total == 0 or ref_total == 0:
            raise HTTPException(status_code=400, detail="No data for one or both registers")

        # Log-likelihood (G2) computation
        results = []
        all_words = set(target_counts.keys()) | set(ref_counts.keys())

        for word in all_words:
            a = target_counts.get(word, 0)  # freq in target
            b = ref_counts.get(word, 0)     # freq in reference
            c = target_total
            d = ref_total

            e1 = c * (a + b) / (c + d)  # expected freq in target
            e2 = d * (a + b) / (c + d)  # expected freq in reference

            if e1 == 0 or e2 == 0:
                continue

            ll = 0.0
            if a > 0:
                ll += 2 * a * math.log(a / e1)
            if b > 0:
                ll += 2 * b * math.log(b / e2)

            # Positive LL = overrepresented in target
            if a / c > b / d:
                results.append({
                    "word": word,
                    "log_likelihood": round(ll, 2),
                    "target_freq": a,
                    "target_per_mil": round((a / c) * 1_000_000, 1),
                    "ref_freq": b,
                    "ref_per_mil": round((b / d) * 1_000_000, 1),
                })

        results.sort(key=lambda x: x["log_likelihood"], reverse=True)
        results = results[:limit]

        return JSONResponse({
            "keywords": results,
            "target": target,
            "reference": reference,
            "target_tokens": target_total,
            "reference_tokens": ref_total,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/trends -- Frequency over time (parliament + dated corpora)
# ---------------------------------------------------------------------------

@app.get("/api/trends")
async def trends(
    q: str = Query(..., min_length=1, description="Search query"),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Frequency of a term per year (for corpora with year metadata)."""
    fts_q = _sanitise_fts_query(q)
    text_match = f"text:{fts_q}"

    db = await _get_db(db_name)
    try:
        sql = (
            "SELECT year, COUNT(*) as cnt "
            "FROM sentences WHERE sentences MATCH ? "
            "AND year != '' "
            "GROUP BY year "
            "ORDER BY year"
        )
        async with db.execute(sql, [text_match]) as cur:
            rows = await cur.fetchall()

        # Also get total sentences per year for normalization
        year_totals_sql = (
            "SELECT year, COUNT(*) FROM sentences WHERE year != '' GROUP BY year ORDER BY year"
        )
        async with db.execute(year_totals_sql) as cur:
            total_rows = await cur.fetchall()

        year_totals = {r[0]: r[1] for r in total_rows}

        data = []
        for row in rows:
            year = row[0]
            count = row[1]
            total = year_totals.get(year, 1)
            data.append({
                "year": year,
                "count": count,
                "total": total,
                "per_million": round((count / total) * 1_000_000, 1) if total > 0 else 0,
            })

        return JSONResponse({"query": q, "trends": data})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/dispersion -- Where in each corpus does a word appear
# ---------------------------------------------------------------------------

@app.get("/api/dispersion")
async def dispersion(
    q: str = Query(..., min_length=1),
    bins: int = Query(20, ge=5, le=100),
    db_name: str | None = Query(None, alias="db", description="Database to query"),
) -> JSONResponse:
    """Dispersion plot: distribution of a term across corpus segments."""
    fts_q = _sanitise_fts_query(q)
    text_match = f"text:{fts_q}"

    db = await _get_db(db_name)
    try:
        # Get rowid positions of matching sentences per corpus
        sql = (
            "SELECT rowid, corpus FROM sentences WHERE sentences MATCH ?"
        )
        async with db.execute(sql, [text_match]) as cur:
            rows = await cur.fetchall()

        # Get max rowid
        async with db.execute("SELECT MAX(rowid) FROM sentences") as cur:
            max_row = (await cur.fetchone())[0] or 1

        # Bin the positions per corpus
        corpus_bins: dict[str, list[int]] = defaultdict(lambda: [0] * bins)
        corpus_hits: dict[str, int] = Counter()

        for rowid, corpus in rows:
            bin_idx = min(int((rowid / max_row) * bins), bins - 1)
            corpus_bins[corpus][bin_idx] += 1
            corpus_hits[corpus] += 1

        result = [
            {"corpus": c, "bins": corpus_bins[c], "total_hits": corpus_hits[c]}
            for c in sorted(corpus_bins.keys())
        ]

        return JSONResponse({"query": q, "dispersion": result, "bin_count": bins})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# POST /api/llm/classify -- LLM pragmatic function classification
# ---------------------------------------------------------------------------

class LLMClassifyRequest(BaseModel):
    sentences: list[str]
    marker: str
    provider: str = "google"  # google, openrouter, openai
    api_key: str = ""
    model: str = ""


@app.post("/api/llm/classify")
async def llm_classify(req: LLMClassifyRequest) -> JSONResponse:
    """Classify pragmatic function of a discourse marker in context using LLM."""
    if not req.api_key:
        raise HTTPException(status_code=400, detail="API key required")
    if not req.sentences:
        raise HTTPException(status_code=400, detail="No sentences provided")

    # Limit to 50 sentences per request
    sentences = req.sentences[:50]

    prompt = f"""You are an expert in Greek linguistics, specifically discourse marker analysis.

For each sentence below, classify the pragmatic function of the discourse marker "{req.marker}".

Possible functions (adapt based on the marker category):
- adversative (contrast/opposition)
- concessive (unexpected outcome)
- corrective (correction of previous statement)
- causal (reason/cause)
- consecutive (result/consequence)
- additive (addition)
- elaborative (further detail)
- topic-shift (changing subject)
- hedging (softening/uncertainty)
- interactional (engaging listener)
- evidential (source of information)
- temporal (time sequence)

Return a JSON array with one object per sentence:
[{{"sentence_index": 0, "function": "adversative", "confidence": 0.9, "explanation_el": "brief explanation in Greek"}}]

Sentences:
"""
    for i, sent in enumerate(sentences):
        prompt += f"\n{i}. {sent}"

    try:
        if req.provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=req.api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(prompt)
            text = response.text
        elif req.provider == "openrouter":
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {req.api_key}",
                        "HTTP-Referer": "https://greek-corpus-workbench.app",
                        "X-Title": "Greek Corpus Workbench",
                    },
                    json={
                        "model": req.model or "google/gemini-2.0-flash-001",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                    timeout=60,
                )
                text = resp.json()["choices"][0]["message"]["content"]
        elif req.provider == "openai":
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.api_key}"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                    timeout=60,
                )
                text = resp.json()["choices"][0]["message"]["content"]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

        # Try to parse JSON from response
        import json as json_module
        # Extract JSON array from response text
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            classifications = json_module.loads(json_match.group())
        else:
            classifications = [{"raw_response": text}]

        return JSONResponse({"classifications": classifications, "marker": req.marker})

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Missing library: {e}. Install with pip.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/llm/analyze -- LLM pattern synthesis
# ---------------------------------------------------------------------------

class LLMAnalyzeRequest(BaseModel):
    query: str
    sample_sentences: list[str]
    frequency_data: dict[str, Any] | None = None
    provider: str = "google"
    api_key: str = ""
    model: str = ""


@app.post("/api/llm/analyze")
async def llm_analyze(req: LLMAnalyzeRequest) -> JSONResponse:
    """LLM synthesis of patterns across concordance results."""
    if not req.api_key:
        raise HTTPException(status_code=400, detail="API key required")

    samples = req.sample_sentences[:30]
    freq_info = ""
    if req.frequency_data:
        import json as json_module
        freq_info = f"\n\nFrequency data:\n{json_module.dumps(req.frequency_data, ensure_ascii=False, indent=2)}"

    prompt = f"""You are an expert in Modern Greek corpus linguistics.

Analyze the usage patterns of "{req.query}" based on the concordance data below.

Write a concise linguistic analysis (200-300 words) covering:
1. Register distribution (where is it most/least frequent and why)
2. Typical syntactic position (sentence-initial, medial, final)
3. Pragmatic functions observed in the examples
4. Notable collocations or co-occurrence patterns
5. Any register-specific variation in meaning or function

Write in English with Greek examples. Be specific and cite examples.
{freq_info}

Sample concordance lines:
"""
    for i, sent in enumerate(samples):
        prompt += f"\n{i + 1}. {sent}"

    try:
        if req.provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=req.api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(prompt)
            analysis = response.text
        elif req.provider == "openrouter":
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {req.api_key}",
                        "HTTP-Referer": "https://greek-corpus-workbench.app",
                        "X-Title": "Greek Corpus Workbench",
                    },
                    json={
                        "model": req.model or "google/gemini-2.0-flash-001",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                    },
                    timeout=60,
                )
                analysis = resp.json()["choices"][0]["message"]["content"]
        elif req.provider == "openai":
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.api_key}"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                    },
                    timeout=60,
                )
                analysis = resp.json()["choices"][0]["message"]["content"]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

        return JSONResponse({"analysis": analysis, "query": req.query})

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Missing library: {e}. Install with pip.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
