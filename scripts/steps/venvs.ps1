# Python virtual environment creation for -Setup.
# Three venvs keep the trees independent: backend (FastAPI), gui (PySide6),
# jupyter (code interpreter).

function Invoke-CreateVenvs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $specs = @(
        @{ Name = 'venv-backend'; Reqs = Join-Path $RepoRoot 'backend\requirements.txt' }
        @{ Name = 'venv-gui';     Reqs = Join-Path $RepoRoot 'gui\requirements.txt' }
        @{ Name = 'venv-jupyter'; Pip  = @('jupyterlab==4.2.6') }
    )
    foreach ($s in $specs) {
        $path = Join-Path $Root $s.Name
        $py   = Join-Path $path 'Scripts\python.exe'
        if (-not (Test-Path $py)) {
            Write-Host "==> Creating $($s.Name)" -ForegroundColor Cyan
            & python -m venv $path
        } else {
            Write-Host "   ok $($s.Name) already exists" -ForegroundColor Green
        }
        & $py -m pip install --upgrade pip
        if ($s.Reqs -and (Test-Path $s.Reqs)) {
            & $py -m pip install -r $s.Reqs
        } elseif ($s.Pip) {
            & $py -m pip install @($s.Pip)
        }
    }
}
