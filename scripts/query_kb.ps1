param(
  [Parameter(Mandatory=$true)]
  [string]$Query,
  [string]$KbDir = "",
  [int]$TopK = 5,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "query_kb.py"
if (-not $KbDir) { $KbDir = Join-Path $Root "kb" }

& $Python $Script --kb-dir $KbDir --query $Query --topk $TopK

