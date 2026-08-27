# One-click start for Lean Formalizer (Windows)
# Double-click start_translator.bat or:
#   powershell -ExecutionPolicy Bypass -File start_translator.ps1
#
# This script does NOT ship machine-specific paths. On first run it copies
# local_config.example.ps1 → local_config.ps1 (mock defaults) if missing.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 1) Prepend elan to Path when present (does not remove existing Path entries)
$elanBin = Join-Path $env:USERPROFILE ".elan\bin"
if (Test-Path $elanBin) {
    if ($env:Path -notlike "*$elanBin*") {
        $env:Path = "$elanBin;" + $env:Path
    }
}

# 2) Local config (optional). Never commit local_config.ps1 / local_config.env.
$config = Join-Path $Root "local_config.ps1"
$example = Join-Path $Root "local_config.example.ps1"
if (-not (Test-Path $config)) {
    if (Test-Path $example) {
        Copy-Item $example $config
        Write-Host "Created local_config.ps1 from example (mock backend)." -ForegroundColor Yellow
        Write-Host "Edit it to set LEAN_PROJECT and LLM keys when ready." -ForegroundColor Yellow
        Write-Host "  notepad `"$config`"" -ForegroundColor Cyan
    } else {
        Write-Host "No local_config.ps1 — starting with process defaults (mock)." -ForegroundColor Yellow
    }
}
if (Test-Path $config) {
    . $config
}

# Also load the portable .env config.  The Python launchers use this file, so
# loading it here keeps double-click and command-line starts consistent.  It
# accepts both normal KEY=VALUE lines and the accidental legacy
# $env:KEY = "VALUE" lines produced by an older auto-config script.
$envConfig = Join-Path $Root "local_config.env"
if (Test-Path $envConfig) {
    foreach ($rawLine in Get-Content -LiteralPath $envConfig) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $key = $null
        $value = $null
        if ($line -match '^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2]
        } elseif ($line -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2]
        }
        if ($null -ne $key) {
            $value = $value.Trim().Trim('"').Trim("'")
            if ($value) { Set-Item -Path "Env:$key" -Value $value }
        }
    }
}

# 3) Defaults that are never machine-hardcoded in the repo
if (-not $env:MEMORY_DIR) {
    $env:MEMORY_DIR = Join-Path $Root "memory"
}
if (-not $env:LLM_BACKEND) {
    $env:LLM_BACKEND = "mock"
}

Write-Host "=== Lean Formalizer ===" -ForegroundColor Green
Write-Host "Root         = $Root"
Write-Host "LLM_BACKEND  = $env:LLM_BACKEND"
Write-Host "LEAN_PROJECT = $env:LEAN_PROJECT"
Write-Host "MEMORY_DIR   = $env:MEMORY_DIR"
Write-Host "lean on Path = $(Get-Command lean -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)"
Write-Host "Starting web UI..."
Write-Host ""

python (Join-Path $Root "run_web.py")
