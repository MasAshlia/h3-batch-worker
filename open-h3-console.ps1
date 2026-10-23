$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Url = 'http://127.0.0.1:8765/'
$HealthUrl = 'http://127.0.0.1:8765/api/state'

function Test-H3Console {
  try {
    $response = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 2
    return ($response.StatusCode -eq 200)
  } catch { return $false }
}

if (-not (Test-H3Console)) {
  $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
  $config = Join-Path $ProjectRoot 'private\config.windows.yaml'
  if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $config)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("H3 production system is not installed or configured.`n`nProject: $ProjectRoot`nRun setup-production.cmd first.", 'H3 Production') | Out-Null
    exit 1
  }
  $logDir = Join-Path $ProjectRoot 'private\logs'
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  $stdout = Join-Path $logDir 'ui-launcher.stdout.log'
  $stderr = Join-Path $logDir 'ui-launcher.stderr.log'
  Start-Process -FilePath $python -ArgumentList @('-m','h3_worker.ui_server','--config','private/config.windows.yaml','--no-open') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-H3Console) { $ready = $true; break }
  }
  if (-not $ready) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("Could not start the H3 console.`n`nCheck the system configuration. Startup log:`n$stderr", 'H3 Console Startup Failed') | Out-Null
    exit 1
  }
}

Start-Process $Url
