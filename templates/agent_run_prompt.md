# Agent Run Prompt (Codex/OpenClaw/CC)

当你使用本 Skill 执行翻译任务时，按以下闭环执行：

1. 运行 `translate_pipeline.py prepare`，生成 `translation.job.json`。
2. 读取 `translation.job.json`，用当前模型完成翻译。
3. 将结果写入 `translation.result.json`（字段：`items[].chunk_id` + `items[].translated_sentences`）。
4. 运行 `translate_pipeline.py validate`，检查是否产生 `repair.job.rN.json`。
5. 若存在 repair job，则生成对应 `repair.result.rN.json` 并运行 `translate_pipeline.py apply-repair`，然后再次 `validate`。
6. 循环至无新增修复任务后，运行 `translate_pipeline.py finalize`，产出 `output` 与 `translation_report.json`。

约束：
- 不引入外部人工步骤。
- 严格遵守 `locked_terms`、`forbid`、占位符一致性。
- 无法确定术语时允许 `[[TERM_UNRESOLVED:<TERM>]]` 收敛。


## Strict Protocol Requirements (2026-04)

- When writing `translation.result.json`, include:
  - `schema_version` (must match job)
  - `job_id` (must match job)
  - `input_sha256` (must match job)
  - `items[].chunk_id`
  - `items[].translated_sentences`
- Run `validate --strict-gate true`; only proceed if it passes.
- If `repair.job.rN.json` exists, write matching `repair.result.rN.json`, then run `apply-repair` and re-run `validate`.
- Run `finalize --strict-gate true` only after `validation.report.json` has `passed=true`.
