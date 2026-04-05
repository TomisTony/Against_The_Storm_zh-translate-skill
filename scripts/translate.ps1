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
  [bool]$OneShot = $true,
  [bool]$ReuseWork = $true,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
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
$JobPath = Join-Path $WorkDir "translation.job.json"
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
  & $Python @args | Out-Host
  return [int]$LASTEXITCODE
}

function Show-RefineGuide {
  param(
    [Parameter(Mandatory=$true)][int]$Round
  )
  $repairJob = Join-Path $WorkDir ("repair.job.r{0}.json" -f $Round)
  $repairResult = Join-Path $WorkDir ("repair.result.r{0}.json" -f $Round)
  Write-Host ""
  Write-Host "One-shot draft has been exported. Follow steps below for refinement:"
  Write-Host "1) Read repair tasks: $repairJob"
  Write-Host "2) Let model write repair result: $repairResult"
  Write-Host "3) Apply repair:"
  Write-Host "   $Python $Script apply-repair --work-dir $WorkDir --result $ResultPath --repair-result $repairResult --round $Round"
  Write-Host "4) Re-validate (strict):"
  Write-Host "   $Python $Script validate --work-dir $WorkDir --result $ResultPath --validation-report $ValidationReportPath --round $($Round + 1) --strict-gate true"
  Write-Host "5) Finalize strict output:"
  Write-Host "   $Python $Script finalize --work-dir $WorkDir --result $ResultPath --validation-report $ValidationReportPath --strict-gate true --output $Output --report $Report --round $($Round + 1)"
}

function Test-CanReuseWork {
  if (-not (Test-Path -LiteralPath $JobPath)) { return $false }
  if (-not (Test-Path -LiteralPath $ResultPath)) { return $false }
  $checkScript = @'
import hashlib
import json
import os
import sys

job_path, source_path, output_path, profile = sys.argv[1:5]
try:
    with open(job_path, "r", encoding="utf-8-sig") as f:
        job = json.load(f)
    src_abs = os.path.abspath(source_path)
    out_abs = os.path.abspath(output_path)
    if os.path.abspath(str(job.get("input", ""))) != src_abs:
        raise SystemExit(1)
    if os.path.abspath(str(job.get("output", ""))) != out_abs:
        raise SystemExit(1)
    if str(job.get("profile", "")) != profile:
        raise SystemExit(1)
    with open(src_abs, "rb") as f:
        src_sha = hashlib.sha256(f.read()).hexdigest().lower()
    if src_sha != str(job.get("input_sha256", "")).lower():
        raise SystemExit(1)
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
'@
  $checkScript | & $Python - $JobPath $Source $Output $Profile | Out-Null
  return ($LASTEXITCODE -eq 0)
}

$shouldPrepare = $true
if ($ReuseWork -and (Test-CanReuseWork)) {
  Write-Host "Reusing existing job/result in work dir: $WorkDir"
  $shouldPrepare = $false
}

if ($shouldPrepare) {
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
}

if (-not (Test-Path -LiteralPath $ResultPath)) {
  Write-Host "Translation result is missing: $ResultPath"
  Write-Host "Please let model fill this file, then rerun this command."
  exit 1
}

$runRound = 1
$validateCode = Invoke-Validate -Round $runRound

if ($Mode -eq "agent") {
  if ($OneShot -and $validateCode -eq 3) {
    Write-Warning "One-shot mode enabled: skip iterative repair and export draft output now."
    Show-RefineGuide -Round $runRound
    $validateCode = 0
    $finalizeStrict = "false"
  } else {
    $finalizeStrict = "true"
  }
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
  $finalizeStrict = "true"
  if ($validateCode -ne 0) {
    if ($validateCode -eq 2) {
      throw "Validate failed due to stale_result. Please regenerate translation.result.json from current translation.job.json."
    }
    if ($OneShot) {
      Write-Warning "CLI one-shot mode enabled: export draft output and stop iterative repair."
      Show-RefineGuide -Round $runRound
      $validateCode = 0
      $finalizeStrict = "false"
    }
  }
  if ($validateCode -ne 0) {
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
  "--strict-gate", $finalizeStrict,
  "--output", $Output,
  "--report", $Report,
  "--round", "$runRound"
) -Label "finalize"
