<#
  Lends this Windows PC's graphics card to the Aves Caesar workflow server on your NAS.

  Unzip aves-inference-node.zip anywhere (or use a clone of the repository), then in PowerShell:
      powershell -ExecutionPolicy Bypass -File <folder>\start-inference-node.ps1

  The first run sets everything up (Python packages, ~1.8 GB of models) and creates a token.
  Put that token and this PC's address into the stack variables on the NAS:
      AVES_REMOTE_URL=http://<this-pc>:8096
      AVES_REMOTE_TOKEN=<token>
  Uses DirectML, which works with NVIDIA, AMD and Intel cards through the normal graphics driver.
#>
param(
    [int]$Port = 8096,
    [string]$Provider = "directml"
)

$ErrorActionPreference = "Stop"

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

function Run {
    # Native commands don't stop the script on failure by themselves; check every exit code.
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE). Nothing else was changed." }
}

# Works both from the unzipped bundle (script at the top) and from a repository clone (server\windows\).
$Root = $null
foreach ($candidate in @($PSScriptRoot, (Join-Path $PSScriptRoot "..\.."))) {
    if (Test-Path (Join-Path $candidate "server\aves_server\inference_node.py")) {
        $Root = (Resolve-Path $candidate).Path
        break
    }
}
if (-not $Root) {
    throw "Can't find the Aves Caesar files next to this script. Unzip the whole aves-inference-node.zip and run the script from inside that folder."
}

$Venv = Join-Path $Root ".venv-node"
$Python = Join-Path $Venv "Scripts\python.exe"
$Models = Join-Path $Root "models"
$TokenFile = Join-Path $Root "node-token.txt"
Step "Using $Root"

if (-not (Test-Path $Python)) {
    Step "Creating a Python environment in $Venv"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Run "Creating the Python environment" { py -3 -m venv $Venv }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        Run "Creating the Python environment" { python -m venv $Venv }
    } else {
        throw "Python 3.11 or newer is required: https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH')."
    }
}
Run "Checking the Python version" { & $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" }

Step "Installing or updating packages"
Run "Updating pip" { & $Python -m pip install --quiet --upgrade pip }
Run "Installing packages" {
    & $Python -m pip install --quiet "numpy>=1.26" "Pillow>=10.0" "pillow-heif>=0.20" "gpxpy>=1.6" "onnxruntime-directml>=1.19" `
        "fastapi>=0.115" "uvicorn>=0.30" "python-multipart>=0.0.9"
}

if (-not (Test-Path (Join-Path $Models "model-build-info.json"))) {
    Step "Downloading the Aves Caesar models (about 1.8 GB)"
    Run "Downloading the models" { & $Python (Join-Path $Root "scripts\models\download_release_models.py") --output-dir $Models }
}

if (-not (Test-Path $TokenFile)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    ([Convert]::ToBase64String($bytes) -replace '[+/=]', '') | Set-Content -NoNewline $TokenFile
}
$Token = (Get-Content $TokenFile -Raw).Trim()

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if (-not (Get-NetFirewallRule -DisplayName "Aves Caesar inference node" -ErrorAction SilentlyContinue)) {
        Step "Allowing port $Port through Windows Firewall (private networks only)"
        New-NetFirewallRule -DisplayName "Aves Caesar inference node" -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Private -Action Allow | Out-Null
    }
} else {
    Write-Host "Tip: run once as Administrator to open port $Port in Windows Firewall automatically." -ForegroundColor Yellow
}

$addresses = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress
Write-Host ""
Write-Host "Set these in the NAS stack's environment variables, then redeploy (use the address on your home network):" -ForegroundColor Green
foreach ($address in $addresses) { Write-Host "  AVES_REMOTE_URL=http://${address}:$Port" }
Write-Host "  AVES_REMOTE_TOKEN=$Token   (also saved in $TokenFile)"
Write-Host ""

$env:PYTHONPATH = "$Root\server;$Root\backend"
Step "Starting the inference node on port $Port (Ctrl+C to stop)"
& $Python -m aves_server.inference_node --models $Models --token $Token --port $Port --provider $Provider
