---
name: against-the-storm-translation-closedloop
description: 妯″瀷涓诲 + 宸ュ叿鍖栨祦姘寸嚎锛圓gainst the Storm锛夈€傝剼鏈礋璐?prepare/validate/apply-repair/finalize锛屾ā鍨嬭礋璐ｇ炕璇戜笌淇缁撴灉鍥炲啓锛涙敮鎸佹湳璇‖绾︽潫銆佸崰浣嶇鏍￠獙銆佸け璐ユ敹鏁涳紝浠ュ強鍩轰簬鈥滆嫳鏂囧師鏂?瀹樻柟涓枃鈥濈殑鑷姩鏈杩唬銆傞€傜敤浜?Codex/OpenClaw/CC 绛変唬鐞嗙幆澧冦€?---

# Against The Storm Translation Closed Loop

## 鐩爣

- 鍦ㄦ棤浜哄伐鏍″鍓嶆彁涓嬶紝瀹炵幇鏈浼樺厛銆佸彲鏀舵暃銆佸彲澶嶇幇鐨勪腑鏂囩炕璇戞祦绋嬨€?
- 闈㈠悜闀挎枃鏈紝閬垮厤閫愯瘝鏌ヨ瀵艰嚧鐨勪綆鏁堢巼銆?
- 缁熶竴鍏ュ彛鑴氭湰锛屼骇鍑鸿瘧鏂?+ 鎶ュ憡 + 鍙寔缁紨杩涚殑鏈琛ㄣ€?

## 鐩綍缁撴瀯

- `translate_pipeline.py`锛氬崗璁伐鍏峰叆鍙ｏ紙`prepare -> validate -> apply-repair -> finalize`锛?- `autotune_terms.py`锛氬熀浜庤嫳鏂囧師鏂?+ 瀹樻柟涓枃鍙傝€冭嚜鍔ㄨ凯浠ｆ湳璇?
- `build_index.py`锛氭瀯寤烘湰鍦?`kb.sqlite` 涓庤涔夌储寮?
- `query_kb.py`锛氭湳璇绱?
- `term_overrides.json`锛氭渶楂樹紭鍏堢骇鏈纭鍒?
- `scripts/*.ps1`锛歅owerShell 蹇嵎鍏ュ彛
- `references/playbook.md`锛氬畬鏁存搷浣滄墜鍐?

## 蹇€熷紑濮?

1. 瀹夎渚濊禆锛歚scripts/setup_env.ps1`
2. 鏋勫缓 KB锛歚scripts/build_kb.ps1 -Input <zh-CN.txt鎴杍son>`
3. 鎵ц缈昏瘧锛歚scripts/translate.ps1 -Mode agent -Input <en.txt> -Output <zh.txt> -KbDir <kb鐩綍>`
4. 鏈夊畼鏂硅瘧鏂囨椂鑷姩杩唬锛歚scripts/autotune.ps1 -Input <en.txt> -Reference <official_zh.txt> -Output <zh.txt> -KbDir <kb鐩綍>`

## 鍏抽敭绾︽潫

- `term_overrides.json` 浼樺厛绾ф渶楂樸€?
- 鍗犱綅绗?`{0}` / `%s` / 鏁板瓧榛樿寮轰竴鑷淬€?
- 鏈鏃犳硶瀹夊叏纭畾鏃朵娇鐢?`[[TERM_UNRESOLVED:<TERM>]]` 鏀舵暃锛堝彲杩借釜锛屼笉鐬庣寽锛夈€?
## 杈撳嚭鐗?

- 璇戞枃锛歚<output>`
- 鎶ュ憡锛歚translation_report.json`
- 鑷姩杩唬鎶ュ憡锛歚autotune_report.json`


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
