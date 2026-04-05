param(
  [Parameter(Mandatory=$true)]
  [Alias("Input")]
  [string]$Source,
  [Parameter(Mandatory=$true)]
  [string]$Output,
  [string]$KbDir = "",
  [string]$Report = "",
  [ValidateSet("codex","api")]
  [string]$Backend = "codex",
  [string]$CodexDir = "",
  [string]$Profile = "balanced",
  [int]$MaxRepairRounds = 2,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "translate_pipeline.py"
$Overrides = Join-Path $Root "term_overrides.json"
if (-not $KbDir) { $KbDir = Join-Path $Root "kb" }
if (-not $Report) {
  $OutDir = Split-Path -Parent $Output
  if (-not $OutDir) { $OutDir = "." }
  $Report = Join-Path $OutDir "translation_report.json"
}

$args = @(
  $Script,
  "--input", $Source,
  "--output", $Output,
  "--kb-dir", $KbDir,
  "--report", $Report,
  "--overrides", $Overrides,
  "--profile", $Profile,
  "--llm-backend", $Backend,
  "--max-repair-rounds", "$MaxRepairRounds"
)
if ($CodexDir) {
  $args += @("--codex-dir", $CodexDir)
}

& $Python @args
