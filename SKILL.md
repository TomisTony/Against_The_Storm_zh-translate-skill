---
name: against-the-storm-translation-closedloop
description: 模型主导 + 工具协议的 Against the Storm 翻译 Skill。脚本只负责 prepare/validate/apply-repair/finalize，模型负责翻译与修复回写。
---

# Against The Storm Translation Skill

## 目标

- 建立可追踪、可复现、可优化的翻译闭环。
- 保障术语一致性和占位符/数字一致性。
- 支持“快速出稿”和“严格终稿”两种节奏。

## 标准流程（模型主导）

1. `prepare`：生成 `translation.job.json`（分块、术语、参数快照）。
2. 模型读取 job，写入 `translation.result.json`。
3. `validate`：校验身份一致性、术语命中、占位符与数字一致性。
4. 如存在 `repair.job.rN.json`，模型写入 `repair.result.rN.json`，再执行 `apply-repair`。
5. `finalize`：输出最终译文和报告。

协议文件：
- `translation.job.json`
- `translation.result.json`
- `validation.report.json`
- `repair.job.rN.json`
- `repair.result.rN.json`

## 快速出稿（默认）

```powershell
.\scripts\translate.ps1 -Mode agent -Source .\test\test.txt -Output .\test\output.fast.txt -Profile fast
```

- 默认 `-OneShot $true`：一轮校验后直接出稿，不阻塞在多轮 repair。
- 默认 `-ReuseWork $true`：复用已有 job/result，避免重复 prepare。

## 严格终稿（发布前）

```powershell
.\scripts\translate.ps1 -Mode agent -Source .\test\test.txt -Output .\test\output.strict.txt -Profile balanced -OneShot $false
```

- 允许多轮 repair，直到严格校验通过后再 finalize。

## 术语与语言约束

- `term_overrides.json` 最高优先级，命中后必须使用 `target`，禁止出现 `forbid`。
- `locked_terms` 为硬约束，必须命中。
- `p1_terms` 为高优先约束，未命中应进入 repair。
- `p2_terms` 为建议约束，仅做 advisory，不阻断。
- 占位符和数字必须与原文一致。
- 无法确定术语时可使用 `[[TERM_UNRESOLVED:<TERM>]]`，但要尽量减少并在修复轮清零。

## 字符集与编码规范（强制）

- 仓库内文本文件统一使用 `UTF-8`（建议 `UTF-8 without BOM`）。
- Python 读写文本必须显式指定编码：
- 读取：优先 `encoding="utf-8-sig"`（兼容历史 BOM 文件）。
- 写入：统一 `encoding="utf-8"`。
- PowerShell 执行前必须设置 UTF-8 控制台编码（已在 `scripts/translate.ps1` 固化）：
- `[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)`
- `[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)`
- 禁止通过不带编码控制的方式直接写中文协议文件（例如不安全的重定向链路），避免把中文写成 `???`。
- 若出现乱码，先排查三件事：
- 文件本体编码是否为 UTF-8。
- 终端输入/输出编码是否为 UTF-8。
- 脚本读写是否显式声明 UTF-8。

## 常见问题

- `stale_result`：`job_id/input_sha256/schema_version` 与当前 job 不一致，需要重写 result。
- `validate` 失败：查看 `validation.report.json` 与 `repair.job.rN.json`。
- 英文残留偏多：补充 `term_overrides.json`，或切换 `-OneShot $false` 进入修复闭环。
