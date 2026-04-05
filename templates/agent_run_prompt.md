# Agent Run Prompt (Codex/OpenClaw/CC)

你在本 Skill 中只做两件事：  
1) 读写协议文件；2) 做翻译与修复决策。  
脚本只做 util，不替你翻译。

## 执行步骤

1. 运行 `translate_pipeline.py prepare` 生成 `translation.job.json`。
2. 读取 job，按 `items[].source_sentences` 逐句翻译。
3. 写入 `translation.result.json`（必须带 `schema_version/job_id/input_sha256`）。
4. 运行 `translate_pipeline.py validate --strict-gate true`。
5. 若产生 `repair.job.rN.json`，只修复列出的句子，写 `repair.result.rN.json`，再运行 `apply-repair` + `validate`。
6. 通过后运行 `translate_pipeline.py finalize --strict-gate true`。

## 翻译硬约束

- 必须遵守 `locked_terms`：命中 `target`，避免 `forbid`。
- 占位符（如 `{0}`、`%s`）与数字必须与原句一致。
- 对 `soft_terms`：在不冲突时优先中文化，不要无故保留英文术语。
- 文风要求：自然中文，可读，不直译腔。
- 无法确定术语时可用 `[[TERM_UNRESOLVED:<TERM>]]`，并在修复阶段优先消除。

## 快速出稿模式说明

- 当流程以 one-shot 运行时，可先输出草稿，再按 `repair.job.rN.json` 做后续精修。
- 即使是快速稿，也要优先保证术语中文化与占位符正确。
