#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query translation KB with hybrid retrieval (FTS + semantic backend)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

from kb_utils import normalize_text, simple_tokens


def load_meta(kb_dir: Path) -> Dict:
    p = kb_dir / "kb_meta.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def build_fts_query(query: str) -> str:
    toks = [t for t in simple_tokens(query) if len(t) > 1]
    if not toks:
        toks = simple_tokens(query)
    if not toks:
        return normalize_text(query)
    return " AND ".join([f"{t}*" for t in toks])


def fts_search(conn: sqlite3.Connection, query: str, topk: int) -> List[Tuple[int, float]]:
    q = build_fts_query(query)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id, bm25(records_fts) AS bm
        FROM records_fts
        JOIN records r ON r.id = records_fts.rowid
        WHERE records_fts MATCH ?
        ORDER BY bm ASC
        LIMIT ?
        """,
        (q, topk),
    )
    return [(int(row[0]), float(row[1])) for row in cur.fetchall()]


def vector_search_st(kb_dir: Path, query: str, topk: int, model_name: str) -> List[Tuple[int, float]]:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    index_path = kb_dir / "kb.faiss"
    ids_path = kb_dir / "kb_ids.npy"
    if not index_path.exists() or not ids_path.exists():
        return []

    model = SentenceTransformer(model_name)
    qv = model.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")

    index = faiss.read_index(str(index_path))
    ids = np.load(ids_path)

    scores, idxs = index.search(qv, topk)
    out = []
    for s, i in zip(scores[0], idxs[0]):
        if i < 0:
            continue
        out.append((int(ids[i]), float(s)))
    return out


def vector_search_tfidf(kb_dir: Path, query: str, topk: int) -> List[Tuple[int, float]]:
    import numpy as np
    from scipy.sparse import load_npz
    import pickle

    vec_path = kb_dir / "kb_tfidf_vectorizer.pkl"
    mat_path = kb_dir / "kb_tfidf.npz"
    ids_path = kb_dir / "kb_tfidf_ids.npy"
    if not vec_path.exists() or not mat_path.exists() or not ids_path.exists():
        return []

    with open(vec_path, "rb") as f:
        vectorizer = pickle.load(f)

    mat = load_npz(str(mat_path))
    ids = np.load(ids_path)

    qv = vectorizer.transform([query])
    sims = (mat @ qv.T).toarray().ravel()  # cosine-like since l2 normalized

    if sims.size == 0:
        return []

    idx = np.argpartition(-sims, min(topk, sims.size) - 1)[:topk]
    idx = idx[np.argsort(-sims[idx])]

    return [(int(ids[i]), float(sims[i])) for i in idx if sims[i] > 0]


def fetch_records(conn: sqlite3.Connection, ids: List[int]) -> Dict[int, Dict]:
    if not ids:
        return {}
    q = ",".join(["?"] * len(ids))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, key, zh, domain, entity, slot, key_norm, de_norm
        FROM records
        WHERE id IN ({q})
        """,
        ids,
    )

    data = {}
    for r in cur.fetchall():
        data[int(r[0])] = {
            "id": int(r[0]),
            "key": r[1],
            "zh": r[2],
            "domain": r[3],
            "entity": r[4],
            "slot": r[5],
            "key_norm": r[6],
            "de_norm": r[7],
        }
    return data


def hybrid_search(
    conn: sqlite3.Connection,
    kb_dir: Path,
    query: str,
    topk: int,
    fts_topk: int,
    vec_topk: int,
    w_fts: float,
    w_vec: float,
    model_name_override: str | None,
    disable_semantic: bool,
) -> List[Tuple[float, Dict]]:
    meta = load_meta(kb_dir)
    semantic = meta.get("semantic", {})
    backend = semantic.get("backend", "none")

    fts_hits = fts_search(conn, query, fts_topk)

    vec_hits: List[Tuple[int, float]] = []
    if not disable_semantic and backend != "none":
        try:
            if backend == "sentence-transformers":
                model_name = model_name_override or semantic.get("model_name")
                vec_hits = vector_search_st(kb_dir, query, vec_topk, model_name)
            elif backend == "tfidf":
                vec_hits = vector_search_tfidf(kb_dir, query, vec_topk)
        except Exception as e:
            print(f"[warn] semantic disabled due to error: {e}")

    k = 60.0
    fused: Dict[int, float] = {}

    for rank, (rid, _) in enumerate(fts_hits, start=1):
        fused[rid] = fused.get(rid, 0.0) + w_fts * (1.0 / (k + rank))

    for rank, (rid, _) in enumerate(vec_hits, start=1):
        fused[rid] = fused.get(rid, 0.0) + w_vec * (1.0 / (k + rank))

    ids = list(fused.keys())
    recs = fetch_records(conn, ids)

    qn = normalize_text(query)
    for rid, rec in recs.items():
        boost = 0.0
        if qn == rec["key_norm"]:
            boost += 1.5
        if qn == rec["de_norm"]:
            boost += 1.2
        if rec["slot"] == "name":
            boost += 0.12
        fused[rid] += boost

    ranked = sorted([(score, recs[rid]) for rid, score in fused.items() if rid in recs], key=lambda x: x[0], reverse=True)
    return ranked[:topk]


def print_results(items: List[Tuple[float, Dict]]) -> None:
    if not items:
        print("No match.")
        return

    for i, (score, rec) in enumerate(items, start=1):
        de = " ".join(x for x in [rec["domain"], rec["entity"]] if x).strip()
        print(f"[{i}] score={score:.4f}")
        print(f"    key : {rec['key']}")
        print(f"    de  : {de if de else '-'}")
        print(f"    slot: {rec['slot'] if rec['slot'] else '-'}")
        print(f"    zh  : {rec['zh']}")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Query translation KB")
    parser.add_argument("--kb-dir", default=str(script_dir / "kb"))
    parser.add_argument("--query", default="")
    parser.add_argument("--topk", type=int, default=5)
    parser.add_argument("--fts-topk", type=int, default=30)
    parser.add_argument("--vec-topk", type=int, default=30)
    parser.add_argument("--w-fts", type=float, default=0.55)
    parser.add_argument("--w-vec", type=float, default=0.45)
    parser.add_argument("--model", default="", help="Override sentence-transformer model name")
    parser.add_argument("--disable-semantic", action="store_true")

    args = parser.parse_args()

    kb_dir = Path(args.kb_dir)
    sqlite_path = kb_dir / "kb.sqlite"
    if not sqlite_path.exists():
        raise FileNotFoundError(f"KB not found: {sqlite_path}. Run build_index.py first.")

    conn = sqlite3.connect(str(sqlite_path))

    if args.query:
        results = hybrid_search(
            conn=conn,
            kb_dir=kb_dir,
            query=args.query,
            topk=max(1, args.topk),
            fts_topk=max(args.topk, args.fts_topk),
            vec_topk=max(args.topk, args.vec_topk),
            w_fts=args.w_fts,
            w_vec=args.w_vec,
            model_name_override=args.model.strip() or None,
            disable_semantic=args.disable_semantic,
        )
        print_results(results)
        conn.close()
        return 0

    print("Interactive mode. Type query and Enter. 'exit' to quit.")
    try:
        while True:
            q = input("\nquery> ").strip()
            if not q:
                continue
            if q.lower() in {"exit", "quit", "q"}:
                break
            results = hybrid_search(
                conn=conn,
                kb_dir=kb_dir,
                query=q,
                topk=max(1, args.topk),
                fts_topk=max(args.topk, args.fts_topk),
                vec_topk=max(args.topk, args.vec_topk),
                w_fts=args.w_fts,
                w_vec=args.w_vec,
                model_name_override=args.model.strip() or None,
                disable_semantic=args.disable_semantic,
            )
            print_results(results)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
