#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build localization KB indexes:
- SQLite (records + FTS5) for lexical retrieval
- Semantic backend:
  1) sentence-transformers + FAISS (preferred)
  2) TF-IDF sparse vectors (fallback)
"""

from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from kb_utils import auto_pick_file, load_localization, normalize_text, parse_key_structure


def build_records(raw: dict) -> list[dict]:
    rows = []
    for key, zh in raw.items():
        k = str(key).replace("\ufeff", "")
        z = str(zh)
        domain, entity, slot, de = parse_key_structure(k)
        key_norm = normalize_text(k)
        de_norm = normalize_text(de)
        text_for_embed = " | ".join(x for x in [k, de, domain, entity, slot, z] if x)
        rows.append(
            {
                "key": k,
                "zh": z,
                "domain": domain,
                "entity": entity,
                "slot": slot,
                "key_norm": key_norm,
                "de_norm": de_norm,
                "text_for_embed": text_for_embed,
            }
        )
    return rows


def create_sqlite(sqlite_path: Path, rows: list[dict]) -> None:
    if sqlite_path.exists():
        sqlite_path.unlink()

    conn = sqlite3.connect(str(sqlite_path))
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            zh TEXT,
            domain TEXT,
            entity TEXT,
            slot TEXT,
            key_norm TEXT,
            de_norm TEXT,
            text_for_embed TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE VIRTUAL TABLE records_fts USING fts5(
            key,
            zh,
            domain,
            entity,
            slot,
            text_for_embed,
            content='records',
            content_rowid='id',
            tokenize='unicode61'
        )
        """
    )

    cur.executemany(
        """
        INSERT INTO records(key, zh, domain, entity, slot, key_norm, de_norm, text_for_embed)
        VALUES(:key, :zh, :domain, :entity, :slot, :key_norm, :de_norm, :text_for_embed)
        """,
        rows,
    )

    cur.execute(
        """
        INSERT INTO records_fts(rowid, key, zh, domain, entity, slot, text_for_embed)
        SELECT id, key, zh, domain, entity, slot, text_for_embed
        FROM records
        """
    )

    cur.execute("CREATE INDEX idx_records_key_norm ON records(key_norm)")
    cur.execute("CREATE INDEX idx_records_de_norm ON records(de_norm)")
    conn.commit()
    conn.close()


def build_semantic_st(kb_dir: Path, sqlite_path: Path, model_name: str, batch_size: int) -> dict:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    conn = sqlite3.connect(str(sqlite_path))
    cur = conn.cursor()
    cur.execute("SELECT id, text_for_embed FROM records ORDER BY id")
    pairs = cur.fetchall()
    conn.close()

    ids = np.array([p[0] for p in pairs], dtype=np.int64)
    texts = [p[1] for p in pairs]

    model = SentenceTransformer(model_name)
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype("float32")

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    faiss.write_index(index, str(kb_dir / "kb.faiss"))
    np.save(kb_dir / "kb_ids.npy", ids)

    return {
        "backend": "sentence-transformers",
        "model_name": model_name,
        "dim": int(dim),
        "count": int(len(ids)),
    }


def build_semantic_tfidf(kb_dir: Path, sqlite_path: Path) -> dict:
    import numpy as np
    from scipy.sparse import save_npz
    from sklearn.feature_extraction.text import TfidfVectorizer

    conn = sqlite3.connect(str(sqlite_path))
    cur = conn.cursor()
    cur.execute("SELECT id, text_for_embed FROM records ORDER BY id")
    pairs = cur.fetchall()
    conn.close()

    ids = np.array([p[0] for p in pairs], dtype=np.int64)
    texts = [p[1] for p in pairs]

    # char n-gram captures typo/morph variations, works well for enum-like keys
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1, norm="l2")
    mat = vectorizer.fit_transform(texts)

    save_npz(str(kb_dir / "kb_tfidf.npz"), mat)
    np.save(kb_dir / "kb_tfidf_ids.npy", ids)
    with open(kb_dir / "kb_tfidf_vectorizer.pkl", "wb") as f:
        pickle.dump(vectorizer, f)

    return {
        "backend": "tfidf",
        "model_name": "tfidf-charwb-3to5",
        "dim": int(mat.shape[1]),
        "count": int(mat.shape[0]),
    }


def maybe_export_parquet(kb_dir: Path, rows: list[dict]) -> str:
    try:
        import pandas as pd

        df = pd.DataFrame(rows)
        out = kb_dir / "records.parquet"
        df.to_parquet(out, index=False)
        return f"Parquet exported: {out}"
    except Exception as e:
        return f"Parquet skipped: {e}"


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build translation KB indexes (SQLite + semantic)")
    parser.add_argument("--input", default=str(script_dir / "zh-CN.txt"))
    parser.add_argument("--kb-dir", default=str(script_dir / "kb"))
    parser.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--skip-semantic", action="store_true")
    parser.add_argument("--semantic-backend", choices=["auto", "st", "tfidf"], default="auto")

    args = parser.parse_args()

    kb_dir = Path(args.kb_dir)
    kb_dir.mkdir(parents=True, exist_ok=True)

    input_file = auto_pick_file(Path(args.input))
    raw = load_localization(input_file)
    rows = build_records(raw)

    sqlite_path = kb_dir / "kb.sqlite"
    create_sqlite(sqlite_path, rows)

    semantic_info = {"backend": "none", "count": 0, "dim": 0, "model_name": ""}
    semantic_error = None

    if not args.skip_semantic:
        if args.semantic_backend in {"auto", "st"}:
            try:
                semantic_info = build_semantic_st(kb_dir, sqlite_path, args.model, args.batch_size)
            except Exception as e:
                semantic_error = str(e)
                if args.semantic_backend == "st":
                    raise

        if semantic_info.get("backend") == "none" and args.semantic_backend in {"auto", "tfidf"}:
            semantic_info = build_semantic_tfidf(kb_dir, sqlite_path)

    meta = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "input_file": str(input_file),
        "entries": len(rows),
        "semantic": semantic_info,
        "semantic_error": semantic_error,
    }
    (kb_dir / "kb_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Input: {input_file}")
    print(f"Entries: {len(rows)}")
    print(f"SQLite: {sqlite_path}")
    print(f"Semantic backend: {semantic_info['backend']}")
    if semantic_error:
        print(f"Semantic fallback reason: {semantic_error}")
    print(maybe_export_parquet(kb_dir, rows))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"Build failed: {e}", file=sys.stderr)
        raise
