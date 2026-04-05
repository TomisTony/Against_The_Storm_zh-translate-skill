param(
  [Parameter(Mandatory=$true)]
  [Alias("Input")]
  [string]$Source,
  [Parameter(Mandatory=$true)]
  [string]$Reference,
  [Parameter(Mandatory=$true)]
  [string]$Output,
  [string]$KbDir = "",
  [string]$Report = "",
  [string]$AutotuneReport = "",
  [ValidateSet("codex","api")]
  [string]$Backend = "codex",
  [string]$CodexDir = "",
  [int]$MaxIters = 3,
  [int]$MaxRepairRounds = 0,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "autotune_terms.py"
$Pipeline = Join-Path $Root "translate_pipeline.py"
$Overrides = Join-Path $Root "term_overrides.json"
if (-not $KbDir) { $KbDir = Join-Path $Root "kb" }
if (-not $Report) {
  $OutDir = Split-Path -Parent $Output
  if (-not $OutDir) { $OutDir = "." }
  $Report = Join-Path $OutDir "translation_report.json"
}
if (-not $AutotuneReport) {
  $OutDir = Split-Path -Parent $Output
  if (-not $OutDir) { $OutDir = "." }
  $AutotuneReport = Join-Path $OutDir "autotune_report.json"
}

$args = @(
  $Script,
  "--input", $Source,
  "--reference", $Reference,
  "--output", $Output,
  "--kb-dir", $KbDir,
  "--report", $Report,
  "--autotune-report", $AutotuneReport,
  "--overrides", $Overrides,
  "--pipeline", $Pipeline,
  "--llm-backend", $Backend,
  "--max-iters", "$MaxIters",
  "--max-repair-rounds", "$MaxRepairRounds"
)
if ($CodexDir) {
  $args += @("--codex-dir", $CodexDir)
}

& $Python @args
