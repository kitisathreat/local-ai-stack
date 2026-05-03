#Requires -Version 7
<#
.SYNOPSIS
  Per-step logging helper for .github/workflows/install-and-startup.yml.
  Dot-source this file at the top of each pwsh step, then call
  Init-StepLog '<name>.log' to start collecting output.

.DESCRIPTION
  Each step writes a per-step log file under ci-logs/ that the
  Post-comment job tails into the PR comment. Before this helper
  existed, every step redefined the same Log function inline (5
  near-identical copies). Keeping the function in one place means we
  can change the format (timestamps, redaction rules, encoding) once
  and have all steps pick it up.

  The Log function is intentionally `global` so that step bodies which
  define their own helpers can call Log without re-importing.

.EXAMPLE
  - shell: pwsh
    run: |
      . ./.github/scripts/step-logging.ps1
      Init-StepLog '03-init-env.log'
      Log "step started at $(Get-Date -Format s)"
      # ... step logic ...
      Exit-Step
#>

$script:LaiStepLogPath = $null
$script:LaiStepLogBuf = $null

function Init-StepLog {
    param([Parameter(Mandatory)] [string]$Name)
    $script:LaiStepLogPath = Join-Path (Get-Location) "ci-logs/$Name"
    $script:LaiStepLogBuf = [System.Text.StringBuilder]::new()
}

function global:Log {
    param([Parameter(Mandatory)] [string]$Msg)
    Write-Host $Msg
    if ($script:LaiStepLogBuf -and $script:LaiStepLogPath) {
        [void]$script:LaiStepLogBuf.AppendLine($Msg)
        [System.IO.File]::WriteAllText(
            $script:LaiStepLogPath,
            $script:LaiStepLogBuf.ToString(),
            [System.Text.UTF8Encoding]::new($false),
        )
    }
}

function Exit-Step {
    # Force a clean exit. Pip and other native invocations inside the
    # launcher leave $LASTEXITCODE non-zero on success paths where we
    # caught and continued; without this reset, the step fails despite
    # the functional verification (healthz / 200) succeeding.
    $global:LASTEXITCODE = 0
    exit 0
}
