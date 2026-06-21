# Svarna — Greek Corpus Workbench

A web-based corpus linguistics platform for Greek, serving three searchable databases (70M+ tokens total) through a single interface.

**Live instance:** [greek-corpus-workbench.wonderfulhill-e1c9f1a0.westeurope.azurecontainerapps.io](https://greek-corpus-workbench.wonderfulhill-e1c9f1a0.westeurope.azurecontainerapps.io/)

## Corpora

| Database | Sentences | Tokens | Sources |
|----------|-----------|--------|---------|
| **corpus** (institutional) | 7.8M | 52M | Wikipedia, Parliament, CC-100, OSCAR, OpenSubtitles, Europarl, Leipzig, Universal Dependencies |
| **literature** | 1.6M | 18.5M | Project Gutenberg Greek (221 books), BabyLM-ell, Interwar Poetry |
| **dialectal** | 543K | 5.9M | GRDD+ (Cretan, Cypriot, Pontic, Tsakonian, Griko, Northern, Eptanisian, Maniot, Katharevousa) |

## Features

- **Concordancer (KWIC):** Full-text search with left/right context, filterable by corpus, register, mode
- **Frequency analysis:** Per-million normalization across registers, collocations with MI scores
- **Discourse markers:** Pre-computed lexicon of 93 Greek markers across 11 categories, with per-register frequency breakdowns and positional distribution
- **Text analysis:** Word frequency lists, TTR, n-grams, dispersion plots, collocation networks, register comparison with log-ratio keyness
- **Regex search:** Raw pattern matching across the entire corpus
- **LLM layer:** BYO-key pragmatic classification and corpus-grounded analysis (Google Gemini / OpenRouter)
- **Multi-database selector:** Switch between corpus, literature, and dialectal databases from the navbar

## Architecture

- **Backend:** FastAPI + aiosqlite, serving SQLite FTS5 indexes with `unicode61` tokenizer
- **Frontend:** Single-file HTML/JS with Chart.js, no build step
- **Deployment:** Docker on Azure Container Apps, databases on Azure File Share

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with a database
DB_PATH=corpus.db uvicorn main:app --port 5001

# Run with all three databases
DB_PATH=corpus.db \
LITERATURE_DB_PATH=literature.db \
DIALECTAL_DB_PATH=dialectal.db \
uvicorn main:app --port 5001
```

## Building the Databases

The databases are not included in the repo (they total ~10 GB). Build them from source:

```bash
# 1. Main corpus (requires raw corpora in ~/greek_corpora/)
python build_index.py

# 2. Literature corpus (downloads from Gutenberg, HuggingFace, GitHub)
python download_literature.py
python build_literature_index.py

# 3. Dialectal corpus (requires GRDD+ text files in grdd_plus/)
python build_dialectal_index.py

# 4. Precompute discourse marker frequencies (run on each .db)
python precompute_markers.py corpus.db
python precompute_markers.py literature.db
python precompute_markers.py dialectal.db
```

## Docker

```bash
docker build -t svarna .
docker run -p 5001:5001 -v /path/to/dbs:/data \
  -e DB_PATH=/data/corpus.db \
  -e LITERATURE_DB_PATH=/data/literature.db \
  -e DIALECTAL_DB_PATH=/data/dialectal.db \
  svarna
```

## License

MIT
