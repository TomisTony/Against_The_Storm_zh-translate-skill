# Against The Storm zh-translate Skill

全自动术语约束翻译 Skill（闭环版本），用于 Against the Storm 相关网页/补丁说明/长文本翻译。

## 特性

- 长文本分块翻译：`ingest -> term_lock -> translate -> validate -> repair`
- 术语硬约束：`term_overrides.json` 最高优先级
- 占位符/数字一致性校验
- 失败可收敛：`[[TERM_UNRESOLVED:<TERM>]]`
- 自动术语迭代：基于英文原文 + 官方中文参考补充术语

## 目录

- `SKILL.md`: Skill 入口说明
- `references/playbook.md`: 操作手册
- `scripts/*.ps1`: 快捷执行脚本
- `translate_pipeline.py`: 主翻译流水线
- `autotune_terms.py`: 自动迭代脚本

## 快速运行

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\translate.ps1 -Source .\input.txt -Output .\output.zh-CN.txt -KbDir .\kb -Backend codex
```

