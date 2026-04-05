#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

RE_EN = re.compile(r"[A-Za-z]")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_UNRESOLVED = re.compile(r"\[\[TERM_UNRESOLVED:")


def normalize_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def split_lines(text: str) -> List[str]:
    return [line for line in text.splitlines() if normalize_line(line)]


def compute_en_only_line_ratio(lines: List[str]) -> float:
    if not lines:
        return 0.0
    en_only = 0
    for line in lines:
        if RE_EN.search(line) and not RE_CJK.search(line):
            en_only += 1
    return en_only / len(lines)


def build_diff_snippets(output_lines: List[str], answer_lines: List[str], max_items: int) -> List[Dict[str, Any]]:
    snippets: List[Dict[str, Any]] = []
    total = max(len(output_lines), len(answer_lines))
    for idx in range(total):
        out_line = output_lines[idx] if idx < len(output_lines) else ""
        ans_line = answer_lines[idx] if idx < len(answer_lines) else ""
        if normalize_line(out_line) == normalize_line(ans_line):
            continue
        snippets.append(
            {
                "line": idx + 1,
                "output": out_line,
                "answer": ans_line,
            }
        )
        if len(snippets) >= max_items:
            break
    return snippets


def compute_term_hit_rate(pipeline_report: Dict[str, Any]) -> float:
    term_total = int(pipeline_report.get("term_total", 0) or 0)
    term_hit = int(pipeline_report.get("term_hit", 0) or 0)
    if term_total <= 0:
        return 0.0
    return term_hit / term_total


def main() -> int:
    p = argparse.ArgumentParser(description="Compare translated output with answer reference.")
    p.add_argument("--output", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--pipeline-report", default="")
    p.add_argument("--max-diffs", type=int, default=8)
    args = p.parse_args()

    output_path = Path(args.output).resolve()
    answer_path = Path(args.answer).resolve()
    report_path = Path(args.report).resolve()
    pipeline_report_path = Path(args.pipeline_report).resolve() if args.pipeline_report else None

    output_text = read_text(output_path)
    answer_text = read_text(answer_path)
    output_lines = split_lines(output_text)
    answer_lines = split_lines(answer_text)
    output_joined = "\n".join(output_lines)
    answer_joined = "\n".join(answer_lines)

    char_similarity = difflib.SequenceMatcher(None, output_text, answer_text).ratio()
    line_similarity = difflib.SequenceMatcher(None, output_joined, answer_joined).ratio()
    en_only_line_ratio = compute_en_only_line_ratio(output_lines)
    unresolved_tags = len(RE_UNRESOLVED.findall(output_text))

    pipeline_report: Dict[str, Any] = {}
    if pipeline_report_path and pipeline_report_path.exists():
        pipeline_report = json.loads(pipeline_report_path.read_text(encoding="utf-8-sig"))
    term_hit_rate = compute_term_hit_rate(pipeline_report)
    combined_score = 0.6 * char_similarity + 0.2 * line_similarity + 0.2 * term_hit_rate

    payload = {
        "version": 1,
        "output_file": str(output_path),
        "answer_file": str(answer_path),
        "pipeline_report_file": str(pipeline_report_path) if pipeline_report_path else "",
        "char_similarity": round(char_similarity, 6),
        "line_similarity": round(line_similarity, 6),
        "term_hit_rate": round(term_hit_rate, 6),
        "en_only_line_ratio": round(en_only_line_ratio, 6),
        "unresolved_tags": unresolved_tags,
        "combined_score": round(combined_score, 6),
        "output_line_count": len(output_lines),
        "answer_line_count": len(answer_lines),
        "diff_snippets": build_diff_snippets(output_lines, answer_lines, int(args.max_diffs)),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Compare report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
