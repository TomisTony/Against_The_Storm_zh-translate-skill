#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from query_kb import hybrid_search

RE_TERM = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})(?: +(?:[A-Z][A-Za-z0-9'+/\-]{2,}|[A-Z]{2,})){0,4}\b"
)
RE_EN_WORD = re.compile(r"[A-Za-z]")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'+/\-]*")
RE_WS = re.compile(r"\s+")

GENERIC_TERMS = {
    "greetings",
    "viceroys",
    "now",
    "if",
    "also",
    "anyway",
    "balance",
    "ux",
    "ui",
    "bug",
    "bugs",
    "fixed",
    "improved",
    "updated",
    "added",
    "removed",
    "the",
    "some",
    "instead",
    "these",
    "this",
    "we",
    "you",
    "year",
    "storm",
    "sale",
    "steam",
    "cornerstone",
    "cornerstones",
    "order",
    "orders",
    "perk",
    "perks",
    "modifier",
    "modifiers",
    "effect",
    "effects",
}
EN_STOPWORDS = {
    "the",
    "of",
    "and",
    "for",
    "with",
    "in",
    "on",
    "to",
    "a",
    "an",
}


def normalize_quotes(text: str) -> str:
    out = text
    out = out.replace("\u2019", "'").replace("\u2018", "'")
    out = out.replace("鈥檚", "'s")
    out = out.replace("鈥�", "'")
    out = out.replace("鈥�", '"')
    return out


def contains_term(text: str, term: str) -> bool:
    esc = re.escape(term)
    if RE_EN_WORD.search(term):
        return bool(re.search(rf"(?i)(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", text))
    return term in text


def contains_cjk(text: str) -> bool:
    return bool(RE_CJK.search(text or ""))


def normalize_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(rule)
    r["id"] = str(r.get("id", "")).strip() or "rule"
    r["source"] = str(r.get("source", "")).strip()
    r["target"] = str(r.get("target", "")).strip()
    r["match"] = str(r.get("match", "exact_ci")).strip() or "exact_ci"
    r["priority"] = int(r.get("priority", 90))
    r["scope"] = str(r.get("scope", "global")).strip() or "global"
    r["enabled"] = bool(r.get("enabled", True))
    forbid = []
    for x in r.get("forbid", []):
        s = str(x).strip()
        if s and s not in forbid and s != r["target"]:
            forbid.append(s)
    r["forbid"] = forbid
    return r


def load_overrides(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for it in raw.get("rules", []):
        r = normalize_rule(it)
        if r["source"] and r["target"] and r["enabled"]:
            out.append(r)
    return out


def save_overrides(path: Path, rules: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps({"version": 1, "rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "term"


def extract_term_candidates(source_text: str) -> List[str]:
    src = normalize_quotes(source_text)
    seen = set()
    raw = []
    for m in RE_TERM.finditer(src):
        term = m.group(0).strip()
        if len(term) < 3:
            continue
        term = RE_WS.sub(" ", term)
        lower = term.lower()
        if lower in GENERIC_TERMS:
            continue
        toks = RE_EN_TOKEN.findall(term)
        core = [t.lower() for t in toks if t.lower() not in EN_STOPWORDS]
        if not core:
            continue
        if all(t in GENERIC_TERMS for t in core):
            continue
        if len(toks) == 1 and toks[0].lower() in GENERIC_TERMS:
            continue
        # Auto tune only multi-word entities to avoid noisy one-word drift.
        if len(toks) < 2:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        raw.append(term)

    raw.sort(key=lambda x: (-len(x.split()), -len(x), x.lower()))
    out = []
    for term in raw:
        lk = term.lower()
        if any(contains_term(k.lower(), lk) for k in out):
            continue
        out.append(term)
    return out


def token_set(text: str) -> set[str]:
    tokens = []
    for t in RE_EN_TOKEN.findall(normalize_quotes(text or "").lower()):
        if t in EN_STOPWORDS:
            continue
        if len(t) <= 2:
            continue
        tokens.append(t)
    return set(tokens)


def term_query_variants(term: str) -> List[str]:
    term_clean = RE_WS.sub(" ", normalize_quotes(term)).strip()
    variants = [term_clean]
    suffixes = [
        " Cornerstone",
        " Order",
        " Perk",
        " Modifier",
        " Effect",
        " Forest Mystery",
        " World Event",
    ]
    for suf in suffixes:
        if term_clean.endswith(suf) and len(term_clean) > len(suf) + 2:
            variants.append(term_clean[: -len(suf)].strip())
    if term_clean.endswith("s") and len(term_clean) > 4:
        variants.append(term_clean[:-1])
    dedup = []
    seen = set()
    for v in variants:
        k = v.lower()
        if not v or k in seen:
            continue
        seen.add(k)
        dedup.append(v)
    return dedup


def query_best_mapping(
    conn: sqlite3.Connection,
    kb_dir: Path,
    term: str,
    reference_text: str,
    topk: int,
    min_score: float,
    min_margin: float,
) -> Tuple[str, float] | None:
    queries = term_query_variants(term)

    best = ("", -1.0)
    second_score = -1.0
    for q in queries:
        q_tokens = token_set(q)
        if not q_tokens:
            continue
        hits = hybrid_search(
            conn=conn,
            kb_dir=kb_dir,
            query=q,
            topk=max(1, topk),
            fts_topk=max(20, topk),
            vec_topk=max(20, topk),
            w_fts=0.55,
            w_vec=0.45,
            model_name_override=None,
            disable_semantic=False,
        )
        for score, rec in hits:
            zh = str(rec.get("zh", "")).strip()
            if not zh or not contains_cjk(zh):
                continue
            slot = str(rec.get("slot", "")).lower()
            if slot not in {"name", "title"}:
                continue
            rec_tokens = token_set(" ".join([str(rec.get("key", "")), str(rec.get("de_norm", "")), str(rec.get("entity", ""))]))
            overlap = len(q_tokens & rec_tokens)
            coverage = overlap / max(1, len(q_tokens))
            if overlap == 0 or coverage < 0.75:
                continue
            if zh not in reference_text:
                continue
            bonus = 0.02
            final_score = float(score) + bonus + (0.03 * coverage)
            if final_score > best[1]:
                second_score = best[1]
                best = (zh, final_score)
            elif final_score > second_score:
                second_score = final_score

    if best[1] < min_score:
        return None
    if second_score >= 0 and (best[1] - second_score) < min_margin:
        return None
    return best


def propose_rules_from_reference(
    source_text: str,
    reference_text: str,
    conn: sqlite3.Connection,
    kb_dir: Path,
    topk: int,
    min_score: float,
    min_margin: float,
) -> List[Dict[str, Any]]:
    proposals = []
    for term in extract_term_candidates(source_text):
        # If official text keeps English as-is, skip this term.
        if contains_term(reference_text, term):
            continue
        best = query_best_mapping(
            conn=conn,
            kb_dir=kb_dir,
            term=term,
            reference_text=reference_text,
            topk=topk,
            min_score=min_score,
            min_margin=min_margin,
        )
        if not best:
            continue
        target, score = best
        forbid = []
        for v in term_query_variants(term):
            if v.lower() == term.lower():
                continue
            if v and v not in forbid:
                forbid.append(v)
        proposals.append(
            {
                "id": f"auto_ref_{slugify(term)}",
                "source": term,
                "target": target,
                "match": "exact_ci",
                "priority": 99,
                "scope": "auto_ref",
                "forbid": forbid,
                "enabled": True,
                "_score": round(score, 6),
            }
        )
    return proposals


def upsert_rules(
    existing_rules: List[Dict[str, Any]],
    proposals: List[Dict[str, Any]],
    force_update: bool,
) -> Tuple[List[Dict[str, Any]], int, int]:
    by_source = {r["source"].lower(): r for r in existing_rules}
    added = 0
    updated = 0
    for p in proposals:
        lk = p["source"].lower()
        cur = by_source.get(lk)
        if not cur:
            rule = {k: v for k, v in p.items() if not k.startswith("_")}
            existing_rules.append(rule)
            by_source[lk] = rule
            added += 1
            continue

        cur_id = str(cur.get("id", "")).lower()
        cur_pri = int(cur.get("priority", 0))
        old_target = str(cur.get("target", "")).strip()
        new_target = str(p.get("target", "")).strip()
        new_forbid = [str(x).strip() for x in p.get("forbid", []) if str(x).strip()]
        cur_forbid = list(cur.get("forbid", []))
        forbid_changed = False
        for bad in new_forbid:
            if bad not in cur_forbid:
                cur_forbid.append(bad)
                forbid_changed = True
        if forbid_changed:
            cur["forbid"] = cur_forbid

        if not new_target or old_target == new_target:
            if forbid_changed:
                updated += 1
            continue

        # Keep user/manual hard rules unless explicitly forced.
        is_auto = cur_id.startswith("auto_")
        if not force_update and (cur_pri >= 100 and not is_auto):
            continue

        cur["target"] = new_target
        forbid = list(cur.get("forbid", []))
        if old_target and old_target != new_target and old_target not in forbid:
            forbid.append(old_target)
        cur["forbid"] = forbid
        updated += 1
    return existing_rules, added, updated


def run_cmd(cmd: List[str], label: str, allow_failure: bool = False) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not allow_failure:
        raise RuntimeError(f"{label} failed.\nCMD: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def run_round_pipeline(
    args: argparse.Namespace,
    round_no: int,
    round_work_dir: Path,
    round_output_path: Path,
    round_report_path: Path,
) -> Dict[str, Any]:
    result_path = round_work_dir / "translation.result.json"
    validation_report_path = round_work_dir / "validation.report.json"
    round_info: Dict[str, Any] = {
        "round": round_no,
        "work_dir": str(round_work_dir),
        "output_file": str(round_output_path),
        "report_file": str(round_report_path),
        "status": "failed",
        "reason": "",
    }

    run_cmd(
        [
            sys.executable,
            str(args.pipeline),
            "prepare",
            "--input",
            str(args.input),
            "--output",
            str(round_output_path),
            "--kb-dir",
            str(args.kb_dir),
            "--report",
            str(round_report_path),
            "--overrides",
            str(args.overrides),
            "--work-dir",
            str(round_work_dir),
            "--clean-work",
            "true",
        ],
        f"prepare.r{round_no}",
    )

    if not result_path.exists():
        round_info["reason"] = f"missing_result:{result_path}"
        return round_info

    repair_round = 1
    code, _, _ = run_cmd(
        [
            sys.executable,
            str(args.pipeline),
            "validate",
            "--work-dir",
            str(round_work_dir),
            "--result",
            str(result_path),
            "--validation-report",
            str(validation_report_path),
            "--round",
            str(repair_round),
            "--strict-gate",
            "true",
        ],
        f"validate.r{round_no}.{repair_round}",
        allow_failure=True,
    )
    if code == 2:
        round_info["reason"] = "stale_result"
        return round_info

    while code == 3:
        if repair_round > int(args.max_repair_rounds):
            round_info["reason"] = f"max_repair_rounds_exceeded:{args.max_repair_rounds}"
            return round_info
        repair_job = round_work_dir / f"repair.job.r{repair_round}.json"
        if not repair_job.exists():
            round_info["reason"] = f"missing_repair_job:{repair_job}"
            return round_info
        repair_result = round_work_dir / f"repair.result.r{repair_round}.json"
        if not repair_result.exists():
            round_info["reason"] = f"missing_repair_result:{repair_result}"
            return round_info
        run_cmd(
            [
                sys.executable,
                str(args.pipeline),
                "apply-repair",
                "--work-dir",
                str(round_work_dir),
                "--result",
                str(result_path),
                "--repair-result",
                str(repair_result),
                "--round",
                str(repair_round),
            ],
            f"apply-repair.r{round_no}.{repair_round}",
        )
        repair_round += 1
        code, _, _ = run_cmd(
            [
                sys.executable,
                str(args.pipeline),
                "validate",
                "--work-dir",
                str(round_work_dir),
                "--result",
                str(result_path),
                "--validation-report",
                str(validation_report_path),
                "--round",
                str(repair_round),
                "--strict-gate",
                "true",
            ],
            f"validate.r{round_no}.{repair_round}",
            allow_failure=True,
        )
        if code == 2:
            round_info["reason"] = "stale_result_after_repair"
            return round_info

    if code != 0:
        round_info["reason"] = f"validate_failed_exit_{code}"
        return round_info

    finalize_code, _, finalize_err = run_cmd(
        [
            sys.executable,
            str(args.pipeline),
            "finalize",
            "--work-dir",
            str(round_work_dir),
            "--result",
            str(result_path),
            "--validation-report",
            str(validation_report_path),
            "--strict-gate",
            "true",
            "--output",
            str(round_output_path),
            "--report",
            str(round_report_path),
            "--round",
            str(repair_round),
        ],
        f"finalize.r{round_no}",
        allow_failure=True,
    )
    if finalize_code != 0:
        round_info["reason"] = f"finalize_failed_exit_{finalize_code}:{finalize_err.strip()}"
        return round_info

    round_info["status"] = "passed"
    round_info["reason"] = ""
    round_info["repair_rounds"] = repair_round
    return round_info


def evaluate_term_misses(source_text: str, output_text: str, proposals: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    misses = []
    for p in proposals:
        src = str(p["source"]).strip()
        tgt = str(p["target"]).strip()
        if not src or not tgt:
            continue
        if not contains_term(source_text, src):
            continue
        if tgt not in output_text:
            misses.append({"source": src, "expected": tgt})
    return misses


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Auto-tune term overrides using source EN + official ZH reference.")
    p.add_argument("--input", required=True, help="Source English input text")
    p.add_argument("--reference", required=True, help="Official Chinese reference text")
    p.add_argument("--output", required=True, help="Pipeline translated output")
    p.add_argument("--report", default=str(script_dir / "translation_report.json"))
    p.add_argument("--autotune-report", default=str(script_dir / "autotune_report.json"))
    p.add_argument("--kb-dir", default=str(script_dir / "kb"))
    p.add_argument("--overrides", default=str(script_dir / "term_overrides.json"))
    p.add_argument("--pipeline", default=str(script_dir / "translate_pipeline.py"))
    p.add_argument("--compare-script", default=str(script_dir / "compare_outputs.py"))
    p.add_argument("--work-dir", default="")
    p.add_argument("--max-repair-rounds", type=int, default=2)
    p.add_argument("--fixed-rounds", type=int, default=5)
    p.add_argument("--kb-topk", type=int, default=8)
    p.add_argument("--min-score", type=float, default=0.12)
    p.add_argument("--min-margin", type=float, default=0.005)
    p.add_argument("--force-update", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).resolve()
    ref_path = Path(args.reference).resolve()
    output_path = Path(args.output).resolve()
    overrides_path = Path(args.overrides).resolve()
    report_path = Path(args.report).resolve()
    autotune_report_path = Path(args.autotune_report).resolve()
    kb_dir = Path(args.kb_dir).resolve()
    compare_script = Path(args.compare_script).resolve()
    base_work_dir = Path(args.work_dir).resolve() if args.work_dir else (output_path.parent / "work")
    sqlite_path = kb_dir / "kb.sqlite"
    if not sqlite_path.exists():
        raise FileNotFoundError(f"KB not found: {sqlite_path}")
    if not compare_script.exists():
        raise FileNotFoundError(f"Compare script not found: {compare_script}")

    source_text = normalize_quotes(input_path.read_text(encoding="utf-8-sig"))
    reference_text = normalize_quotes(ref_path.read_text(encoding="utf-8-sig"))

    conn = sqlite3.connect(str(sqlite_path))
    rounds: List[Dict[str, Any]] = []
    try:
        for i in range(1, int(args.fixed_rounds) + 1):
            t0 = time.time()
            existing_rules = load_overrides(overrides_path)
            proposals = propose_rules_from_reference(
                source_text=source_text,
                reference_text=reference_text,
                conn=conn,
                kb_dir=kb_dir,
                topk=int(args.kb_topk),
                min_score=float(args.min_score),
                min_margin=float(args.min_margin),
            )
            rules, added, updated = upsert_rules(existing_rules, proposals, force_update=bool(args.force_update))
            save_overrides(overrides_path, rules)

            round_work_dir = base_work_dir / f"r{i}"
            round_output_path = output_path.with_name(f"{output_path.stem}.r{i}{output_path.suffix}")
            round_report_path = report_path.with_name(f"{report_path.stem}.r{i}{report_path.suffix}")
            round_info = run_round_pipeline(args, i, round_work_dir, round_output_path, round_report_path)

            compare_report_path = base_work_dir / f"compare.report.r{i}.json"
            compare_payload: Dict[str, Any] = {}
            if round_info.get("status") == "passed":
                run_cmd(
                    [
                        sys.executable,
                        str(compare_script),
                        "--output",
                        str(round_output_path),
                        "--answer",
                        str(ref_path),
                        "--report",
                        str(compare_report_path),
                        "--pipeline-report",
                        str(round_report_path),
                    ],
                    f"compare.r{i}",
                )
                compare_payload = json.loads(compare_report_path.read_text(encoding="utf-8-sig"))
                out_text = round_output_path.read_text(encoding="utf-8-sig")
                misses = evaluate_term_misses(source_text, out_text, proposals)
            else:
                misses = []

            round_info.update({
                "proposed": len(proposals),
                "added": added,
                "updated": updated,
                "misses": len(misses),
                "sample_misses": misses[:20],
                "compare_report": str(compare_report_path) if compare_report_path.exists() else "",
                "compare_metrics": compare_payload,
                "latency_ms": int((time.time() - t0) * 1000),
            })
            rounds.append(round_info)
    finally:
        conn.close()

    passed_rounds = [r for r in rounds if r.get("status") == "passed"]
    best_round = None
    if passed_rounds:
        best_round = max(
            passed_rounds,
            key=lambda r: float(((r.get("compare_metrics") or {}).get("combined_score", 0.0))),
        )
        shutil.copy2(Path(best_round["output_file"]), output_path)
        shutil.copy2(Path(best_round["report_file"]), report_path)

    pipeline_report: Dict[str, Any] = {}
    if best_round and report_path.exists():
        pipeline_report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    final = {
        "input": str(input_path),
        "reference": str(ref_path),
        "output": str(output_path),
        "overrides": str(overrides_path),
        "fixed_rounds": int(args.fixed_rounds),
        "rounds": rounds,
        "best_round": best_round.get("round") if best_round else None,
        "best_round_score": (best_round.get("compare_metrics") or {}).get("combined_score") if best_round else None,
        "final_pipeline_metrics": {
            "term_hit": pipeline_report.get("term_hit"),
            "term_total": pipeline_report.get("term_total"),
            "term_unresolved": pipeline_report.get("term_unresolved"),
            "placeholder_errors": pipeline_report.get("placeholder_errors"),
        },
    }
    final["status"] = "passed" if best_round else "failed"
    if not best_round:
        final["failure_reason"] = "no_round_passed_strict_gate"
    autotune_report_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Autotune report: {autotune_report_path}")
    return 0 if best_round else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"Autotune failed: {e}", file=sys.stderr)
        raise
