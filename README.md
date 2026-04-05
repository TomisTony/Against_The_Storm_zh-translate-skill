# Against The Storm zh-translate Skill

妯″瀷涓诲 + 鏈绾︽潫鐨勭炕璇?Skill锛岀敤浜?Against the Storm 鐩稿叧缃戦〉銆佽ˉ涓佽鏄庛€侀暱鏂囨湰缈昏瘧銆?
## 鐗规€?
- 妯″瀷涓诲缂栨帓锛氳剼鏈彧璐熻矗 `prepare/validate/apply-repair/finalize` 宸ュ叿娴佺▼
- 鏈纭害鏉燂細`term_overrides.json` 鏈€楂樹紭鍏堢骇
- 鍗犱綅绗?鏁板瓧涓€鑷存€ф牎楠?- 澶辫触鍙敹鏁涳細`[[TERM_UNRESOLVED:<TERM>]]`
- 鑷姩鏈杩唬锛氬熀浜庤嫳鏂囧師鏂?+ 瀹樻柟涓枃鍙傝€冭ˉ鍏呮湳璇?- 鍗忚鏂囦欢缁熶竴锛歚translation.job.json / translation.result.json / validation.report.json / repair.result.rN.json`

## 鐩綍

- `SKILL.md`: Skill 鍏ュ彛璇存槑
- `references/playbook.md`: 鎿嶄綔鎵嬪唽
- `scripts/*.ps1`: 蹇嵎鎵ц鑴氭湰
- `translate_pipeline.py`: 涓荤炕璇戞祦姘寸嚎
- `autotune_terms.py`: 鑷姩杩唬鑴氭湰


## 蹇€熻繍琛岋紙妯″瀷涓诲锛?
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\translate.ps1 -Mode agent -Source .\input.txt -Output .\output.zh-CN.txt -KbDir .\kb
```

娴佺▼璇存槑锛?
1. 鑴氭湰浼氬厛鐢熸垚 `work/translation.job.json`銆? 
2. 鐢辨ā鍨嬭鍙?job 骞跺啓鍏?`work/translation.result.json`銆? 
3. 鑴氭湰鎵ц `validate`锛岃嫢鏈変慨澶嶄换鍔′細鐢熸垚 `repair.job.rN.json`銆? 
4. 鐢辨ā鍨嬪啓鍏?`repair.result.rN.json`锛岃剼鏈?`apply-repair` 鍚庣户缁獙璇併€? 
5. 鏈€缁?`finalize` 杈撳嚭璇戞枃鍜?`translation_report.json`銆? 

## Strict Gate Update (2026-04)

- `prepare` now writes stable identity fields into `translation.job.json`:
  - `schema_version`
  - `job_id`
  - `input_sha256`
- `translation.result.json` must contain matching `schema_version/job_id/input_sha256`.
- `validate` now writes `passed` and returns non-zero under strict gate when:
  - identity mismatch (`stale_result`, exit 2), or
  - `violation_count > 0` / `repair_task_count > 0` (exit 3).
- `finalize` now requires latest `validation.report.json` with `passed=true`.
- `finalize` strict gate checks:
  - `term_unresolved == 0`
  - `placeholder_errors == 0`
  - `en_only_line_ratio <= 0.10`

### New Compare Utility

- Added `compare_outputs.py` and `compare.report.rN.json`.
- Metrics:
  - `char_similarity`
  - `line_similarity`
  - `term_hit_rate`
  - `en_only_line_ratio`
  - `unresolved_tags`
- Combined score formula:
  - `0.6 * char_similarity + 0.2 * line_similarity + 0.2 * term_hit_rate`

### Fixed 5-Round Autotune

- `autotune_terms.py` now runs fixed rounds (`--fixed-rounds`, default `5`).
- Each round uses isolated workspace: `work/r1 ... work/r5`.
- Only rounds passing strict gate participate in final selection.
- Best successful round output is copied to requested `--output` and `--report`.
