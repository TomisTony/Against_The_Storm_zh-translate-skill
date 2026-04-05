# Playbook

## 1) 鐜鍑嗗

```powershell
cd <skill鐩綍>
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
```

## 2) 鏋勫缓鐭ヨ瘑搴擄紙涓€娆″嵆鍙級

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_kb.ps1 `
  -Input .\zh-CN.txt `
  -KbDir .\kb `
  -SemanticBackend auto
```

璇存槑锛?
- `Bootstrap` 鍙湪棣栨鏃犺鍒欐垨鏄惧紡 `--bootstrap-force` 鏃惰Е鍙戯紝涓嶄細姣忔閲嶈窇閮介噸寤鸿鍒欍€?
- `term_overrides.json` 涓?`term_drift_history.json` 閮芥槸鎸佷箙鍖栨枃浠躲€?

## 3) 涓荤炕璇戞祦绋?
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\translate.ps1 `
  -Input .\input_en.txt `
  -Output .\output_zh.txt `
  -KbDir .\kb `
  -Mode agent
```

妯″瀷涓诲鍗忚璇存槑锛?- `prepare` 鐢熸垚 `work/translation.job.json`銆?- 妯″瀷鏍规嵁 job 鍐欏叆 `work/translation.result.json`銆?- `validate` 鐢熸垚 `work/validation.report.json`锛屽苟鍦ㄩ渶瑕佹椂鐢熸垚 `work/repair.job.rN.json`銆?- 妯″瀷鍐欏叆 `work/repair.result.rN.json` 鍚庯紝鎵ц `apply-repair` 骞剁户缁?`validate`銆?- `finalize` 杈撳嚭鏈€缁堣瘧鏂囧拰 `translation_report.json`銆?
## 4) 鑷姩鏈杩唬锛堟湁瀹樻柟璇戞枃鏃讹級

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\autotune.ps1 `
  -Input .\input_en.txt `
  -Reference .\official_zh.txt `
  -Output .\output_zh.txt `
  -KbDir .\kb `
  -FixedRounds 5
```

鏈哄埗锛?
- 浠庤嫳鏂囧師鏂囨娊鍙栧璇嶄笓鍚嶅€欓€夈€?
- 鐢?`kb.sqlite` 妫€绱㈠€欓€変腑鏂囷紝浠呬繚鐣欓珮缃俊涓斿嚭鐜板湪瀹樻柟璇戞枃涓殑鏄犲皠銆?
- 鑷姩鍐欏叆/鏇存柊 `term_overrides.json` 骞堕噸璺戠炕璇戯紝鐩村埌鏀舵暃鎴栬揪鍒拌凯浠ｄ笂闄愩€?

## 5) 榛樿鍙傛暟锛坆alanced锛?

| 鍙傛暟 | 榛樿鍊?|
|---|---:|
| `chunk_chars` | 600 |
| `chunk_chars_min` | 450 |
| `chunk_chars_max` | 700 |
| `batch_chunks` | 4 |
| `kb_topk` | 5 |
| `lock_score_threshold` | 0.35 |
| `lock_margin_threshold` | 0.03 |
| `max_repair_rounds` | 2 |
| `promotion_min_frequency` | 5 |
| `promotion_pass_runs` | 3 |
| `unresolved_policy` | `keep_en_with_tag` |
| `placeholder_strict` | `true` |

## 6) 楠屾敹寤鸿

- `translation_report.json` 鍐咃細
  - `term_hit / term_total` 灏介噺鎺ヨ繎 100%
  - `term_unresolved = 0`锛堟垨浠呬繚鐣欏彲鎺ュ彈灏戦噺锛?
  - `placeholder_errors = 0`
  - `violations` 鍙拷韪笖鏃犻潤榛樺け璐?


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
