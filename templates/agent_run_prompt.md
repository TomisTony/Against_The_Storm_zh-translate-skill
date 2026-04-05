# Agent Run Prompt (Codex/OpenClaw/CC)

当你使用本 Skill 执行翻译任务时，按以下闭环执行：

1. 运行 `translate_pipeline.py`（`--llm-backend codex`）。
2. 如果脚本生成 `codex.translation.job.json`，立即读取该文件并用当前模型完成翻译。
3. 将结果写入 `codex.translation.result.json`，格式必须匹配 job 中 `format`。
4. 重新运行 `translate_pipeline.py`。
5. 若出现 `codex.repair.rN.job.json`，同样生成对应 `codex.repair.rN.result.json` 并重跑。
6. 直到产出 `output` 与 `translation_report.json`。

约束：
- 不引入外部人工步骤。
- 严格遵守 `locked_terms`、`forbid`、占位符一致性。
- 无法确定术语时允许 `[[TERM_UNRESOLVED:<TERM>]]` 收敛。

