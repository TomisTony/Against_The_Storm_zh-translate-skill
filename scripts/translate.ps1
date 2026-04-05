param(
  [Parameter(Mandatory=$true)]
  [Alias("Input")]
  [string]$Source,
  [Parameter(Mandatory=$true)]
  [string]$Output,
  [ValidateSet("agent","cli")]
  [string]$Mode = "agent",
  [string]$KbDir = "",
  [string]$Report = "",
  [string]$WorkDir = "",
  [string]$ResultPath = "",
  [string]$RepairResultPath = "",
  [string]$Backend = "",
  [string]$Profile = "balanced",
  [int]$MaxRepairRounds = 2,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "translate_pipeline.py"
$Overrides = Join-Path $Root "term_overrides.json"
if (-not $KbDir) { $KbDir = Join-Path $Root "kb" }
$OutDir = Split-Path -Parent $Output
if (-not $OutDir) { $OutDir = "." }
if (-not $Report) {
  $Report = Join-Path $OutDir "translation_report.json"
}
if (-not $WorkDir) { $WorkDir = Join-Path $OutDir "work" }
if (-not $ResultPath) { $ResultPath = Join-Path $WorkDir "translation.result.json" }
if ($Backend) {
  Write-Warning "Parameter -Backend is deprecated and ignored. This script now uses protocol files (model-orchestrated mode)."
}
$ValidationReportPath = Join-Path $WorkDir "validation.report.json"

function Invoke-PipelineStep {
  param(
    [Parameter(Mandatory=$true)][string[]]$Args,
    [Parameter(Mandatory=$true)][string]$Label
  )
  & $Python @Args
  if ($LASTEXITCODE -ne 0) {
    throw "$Label failed with exit code $LASTEXITCODE"
  }
}

function Invoke-Validate {
  param(
    [Parameter(Mandatory=$true)][int]$Round
  )
  $args = @(
    $Script, "validate",
    "--work-dir", $WorkDir,
    "--result", $ResultPath,
    "--validation-report", $ValidationReportPath,
    "--round", "$Round",
    "--strict-gate", "true"
  )
  & $Python @args
  return $LASTEXITCODE
}

$prepareArgs = @(
  $Script,
  "prepare",
  "--input", $Source,
  "--output", $Output,
  "--kb-dir", $KbDir,
  "--report", $Report,
  "--overrides", $Overrides,
  "--profile", $Profile,
  "--work-dir", $WorkDir,
  "--max-repair-rounds", "$MaxRepairRounds",
  "--clean-work", "true"
)
Invoke-PipelineStep -Args $prepareArgs -Label "prepare"

if (-not (Test-Path -LiteralPath $ResultPath)) {
  Write-Host "Translation result is missing: $ResultPath"
  Write-Host "Please let model fill this file, then rerun this command."
  exit 1
}

$runRound = 1
$validateCode = Invoke-Validate -Round $runRound

if ($Mode -eq "agent") {
  while ($validateCode -ne 0) {
    if ($validateCode -eq 2) {
      throw "Validate failed due to stale_result. Please regenerate translation.result.json from current translation.job.json."
    }
    if ($validateCode -ne 3) {
      throw "Validate failed with unexpected exit code: $validateCode"
    }
    $repairJob = Join-Path $WorkDir ("repair.job.r{0}.json" -f $runRound)
    if (-not (Test-Path -LiteralPath $repairJob)) {
      throw "Validate failed but repair job was not generated: $repairJob"
    }
    if ($runRound -gt $MaxRepairRounds) {
      throw "Reached max repair rounds ($MaxRepairRounds) but validation still failed."
    }

    $expectedRepair = Join-Path $WorkDir ("repair.result.r{0}.json" -f $runRound)
    if ($runRound -eq 1 -and $RepairResultPath) { $expectedRepair = $RepairResultPath }
    if (-not (Test-Path -LiteralPath $expectedRepair)) {
      Write-Host "Repair result is missing: $expectedRepair"
      Write-Host "Please let model fill this file, then rerun this command."
      exit 1
    }

    Invoke-PipelineStep -Args @(
      $Script, "apply-repair",
      "--work-dir", $WorkDir,
      "--result", $ResultPath,
      "--repair-result", $expectedRepair,
      "--round", "$runRound"
    ) -Label "apply-repair"
    $runRound += 1
    $validateCode = Invoke-Validate -Round $runRound
  }
} else {
  if ($validateCode -ne 0) {
    if ($validateCode -eq 2) {
      throw "Validate failed due to stale_result. Please regenerate translation.result.json from current translation.job.json."
    }
    if (-not $RepairResultPath -or -not (Test-Path -LiteralPath $RepairResultPath)) {
      throw "Validate failed and no valid -RepairResultPath was provided."
    }
    Invoke-PipelineStep -Args @(
      $Script, "apply-repair",
      "--work-dir", $WorkDir,
      "--result", $ResultPath,
      "--repair-result", $RepairResultPath,
      "--round", "$runRound"
    ) -Label "apply-repair"
    $runRound += 1
    $validateCode = Invoke-Validate -Round $runRound
    if ($validateCode -ne 0) {
      throw "Validate still failed after applying repair in cli mode."
    }
  }
}

Invoke-PipelineStep -Args @(
  $Script, "finalize",
  "--work-dir", $WorkDir,
  "--result", $ResultPath,
  "--validation-report", $ValidationReportPath,
  "--strict-gate", "true",
  "--output", $Output,
  "--report", $Report,
  "--round", "$runRound"
) -Label "finalize"
