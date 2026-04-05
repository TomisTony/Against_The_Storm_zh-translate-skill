---
name: against-the-storm-translation-closedloop
description: 全自动术语约束翻译流水线（Against the Storm）。支持长文本分块翻译、术语硬约束、占位符校验、自动修复、失败收敛，以及基于“英文原文+官方中文”的自动术语迭代。适用于 Codex/OpenClaw/CC 等代理环境。
---

# Against The Storm Translation Closed Loop

## 目标

- 在无人工校对前提下，实现术语优先、可收敛、可复现的中文翻译流程。
- 面向长文本，避免逐词查询导致的低效率。
- 统一入口脚本，产出译文 + 报告 + 可持续演进的术语表。

## 目录结构

- `translate_pipeline.py`：主流水线（`ingest -> term_lock -> translate -> validate -> repair`）
- `autotune_terms.py`：基于英文原文 + 官方中文参考自动迭代术语
- `build_index.py`：构建本地 `kb.sqlite` 与语义索引
- `query_kb.py`：术语检索
- `term_overrides.json`：最高优先级术语硬规则
- `scripts/*.ps1`：PowerShell 快捷入口
- `references/playbook.md`：完整操作手册

## 快速开始

1. 安装依赖：`scripts/setup_env.ps1`
2. 构建 KB：`scripts/build_kb.ps1 -Input <zh-CN.txt或json>`
3. 执行翻译：`scripts/translate.ps1 -Input <en.txt> -Output <zh.txt> -KbDir <kb目录>`
4. 有官方译文时自动迭代：`scripts/autotune.ps1 -Input <en.txt> -Reference <official_zh.txt> -Output <zh.txt> -KbDir <kb目录>`

## 关键约束

- `term_overrides.json` 优先级最高。
- 占位符 `{0}` / `%s` / 数字默认强一致。
- 术语无法安全确定时使用 `[[TERM_UNRESOLVED:<TERM>]]` 收敛（可追踪，不瞎猜）。
- `codex` 后端缺少 repair 结果时不会中断，会继续完成主流程并写报告。

## 输出物

- 译文：`<output>`
- 报告：`translation_report.json`
- 自动迭代报告：`autotune_report.json`

