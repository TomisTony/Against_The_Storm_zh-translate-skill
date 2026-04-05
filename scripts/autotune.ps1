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
  [string]$WorkDir = "",
  [int]$FixedRounds = 5,
  [int]$MaxRepairRounds = 2,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "autotune_terms.py"
$Pipeline = Join-Path $Root "translate_pipeline.py"
$CompareScript = Join-Path $Root "compare_outputs.py"
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
if (-not $WorkDir) {
  $OutDir = Split-Path -Parent $Output
  if (-not $OutDir) { $OutDir = "." }
  $WorkDir = Join-Path $OutDir "work"
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
  "--compare-script", $CompareScript,
  "--work-dir", $WorkDir,
  "--fixed-rounds", "$FixedRounds",
  "--max-repair-rounds", "$MaxRepairRounds"
)

& $Python @args
if ($LASTEXITCODE -ne 0) {
  throw "autotune_terms.py failed with exit code $LASTEXITCODE"
}
