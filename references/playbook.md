# Playbook

## 1) 环境准备

```powershell
cd <skill目录>
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
```

## 2) 构建知识库（一次即可）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_kb.ps1 `
  -Input .\zh-CN.txt `
  -KbDir .\kb `
  -SemanticBackend auto
```

说明：
- `Bootstrap` 只在首次无规则或显式 `--bootstrap-force` 时触发，不会每次重跑都重建规则。
- `term_overrides.json` 与 `term_drift_history.json` 都是持久化文件。

## 3) 主翻译流程

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\translate.ps1 `
  -Input .\input_en.txt `
  -Output .\output_zh.txt `
  -KbDir .\kb `
  -Backend codex
```

`codex` 后端说明：
- 若不存在 `codex.translation.result.json`，脚本会先写 `codex.translation.job.json` 并提示补齐结果再重跑。
- repair 阶段若缺少 `codex.repair.rN.result.json`，流程会跳过 repair 并继续收敛，不会整体失败。

## 4) 自动术语迭代（有官方译文时）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\autotune.ps1 `
  -Input .\input_en.txt `
  -Reference .\official_zh.txt `
  -Output .\output_zh.txt `
  -KbDir .\kb `
  -MaxIters 3
```

机制：
- 从英文原文抽取多词专名候选。
- 用 `kb.sqlite` 检索候选中文，仅保留高置信且出现在官方译文中的映射。
- 自动写入/更新 `term_overrides.json` 并重跑翻译，直到收敛或达到迭代上限。

## 5) 默认参数（balanced）

| 参数 | 默认值 |
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

## 6) 验收建议

- `translation_report.json` 内：
  - `term_hit / term_total` 尽量接近 100%
  - `term_unresolved = 0`（或仅保留可接受少量）
  - `placeholder_errors = 0`
  - `violations` 可追踪且无静默失败

