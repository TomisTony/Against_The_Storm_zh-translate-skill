# Playbook

## 1) 环境准备

```powershell
cd <repo目录>
powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
```

## 2) 构建知识库（首次或数据更新后）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_kb.ps1 -Input .\zh-CN.txt -KbDir .\kb
```

## 3) 主翻译流程（模型主导）

```powershell
.\scripts\translate.ps1 -Mode agent -Source .\input_en.txt -Output .\output_zh.txt -KbDir .\kb -Profile fast
```

执行语义：

1. `prepare` 生成 `translation.job.json`
2. 模型回写 `translation.result.json`
3. `validate` 生成 `validation.report.json`
4. 若需要修复，模型回写 `repair.result.rN.json`，再 `apply-repair`
5. `finalize` 产出译文与报告

## 4) 一轮出稿后精修

当 one-shot 输出草稿后，按以下顺序执行：

```powershell
python .\translate_pipeline.py apply-repair --work-dir .\test\work --result .\test\work\translation.result.json --repair-result .\test\work\repair.result.r1.json --round 1
python .\translate_pipeline.py validate --work-dir .\test\work --result .\test\work\translation.result.json --validation-report .\test\work\validation.report.json --round 2 --strict-gate true
python .\translate_pipeline.py finalize --work-dir .\test\work --result .\test\work\translation.result.json --validation-report .\test\work\validation.report.json --strict-gate true --output .\test\output.strict.txt --report .\test\translation_report.strict.json --round 2
```

## 5) 自动术语迭代（可选）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\autotune.ps1 -Input .\test\test.txt -Reference .\test\answer.txt -Output .\test\output.autotune.txt -KbDir .\kb
```

固定 5 轮，独立工作目录，最后在“通过严格闸门”的轮次中选最优。
