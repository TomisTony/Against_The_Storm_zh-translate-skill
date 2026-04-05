param(
  [Alias("Input")]
  [string]$Source = "",
  [string]$KbDir = "",
  [ValidateSet("auto","st","tfidf")]
  [string]$SemanticBackend = "auto",
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "build_index.py"
if (-not $Source) { $Source = Join-Path $Root "zh-CN.txt" }
if (-not $KbDir) { $KbDir = Join-Path $Root "kb" }

Write-Host "[build_kb] input: $Source"
Write-Host "[build_kb] kb: $KbDir"
& $Python $Script --input $Source --kb-dir $KbDir --semantic-backend $SemanticBackend
