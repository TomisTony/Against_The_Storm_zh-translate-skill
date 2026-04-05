# Against The Storm zh-translate Skill

模型主导翻译 Skill。脚本只负责协议化工具流程：

- `prepare`
- `validate`
- `apply-repair`
- `finalize`

## 核心特性

- 术语约束：`term_overrides.json` + `locked_terms`
- 一致性校验：占位符、数字、identity
- 严格闸门：防止带病产出
- 快速出稿：one-shot 模式可一轮出草稿

## 快速开始

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
.\scripts\translate.ps1 -Mode agent -Source .\test\test.txt -Output .\test\output.fast.txt -KbDir .\kb -Profile fast
```

## 两种运行模式

### 1) 快速出稿（默认）

```powershell
.\scripts\translate.ps1 -Mode agent -Source .\test\test.txt -Output .\test\output.fast.txt -Profile fast
```

- 默认 `-OneShot $true`
- 默认 `-ReuseWork $true`
- 目标：低时延快速产出

### 2) 严格终稿

```powershell
.\scripts\translate.ps1 -Mode agent -Source .\test\test.txt -Output .\test\output.strict.txt -Profile balanced -OneShot $false
```

- 启用多轮 repair，直到严格通过
- 目标：发布级质量

## 协议文件

- `translation.job.json`
- `translation.result.json`
- `validation.report.json`
- `repair.job.rN.json`
- `repair.result.rN.json`

## 严格闸门（strict）

- `validate` 失败会返回非 0
- `finalize --strict-gate true` 要求：
  - `term_unresolved == 0`
  - `placeholder_errors == 0`
  - `en_only_line_ratio <= 0.10`

## 评估对比

使用 `compare_outputs.py` 生成对比报告：

```powershell
python .\compare_outputs.py --output .\test\output.fast.txt --answer .\test\answer.txt --report .\test\compare.report.json --pipeline-report .\test\translation_report.json
```
