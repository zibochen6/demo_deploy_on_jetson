$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
if (-not $RootDir) {
  $RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

Set-Location (Join-Path $RootDir "pc_server")

function Test-PythonCmd {
  param([string[]]$Cmd)
  try {
    & $Cmd -V *> $null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Resolve-Python {
  param([string]$UserBin)

  if ($UserBin) {
    if (Test-Path $UserBin) { $cmd = ,@($UserBin) }
    elseif (Get-Command $UserBin -ErrorAction SilentlyContinue) { $cmd = ,@($UserBin) }
    else { throw "PY_BIN '$UserBin' not found. Set PY_BIN to a Python 3.10 executable." }

    $ver = Get-PythonVersion -Cmd $cmd
    if ($ver -ne "3.10") {
      throw "Python $ver detected. This project requires Python 3.10. Set PY_BIN to Python 3.10."
    }
    return $cmd
  }

  function Find-Python310 {
    $candidates = @(
      @("py","-3.10"),
      @("python3.10"),
      @("python")
    )

    foreach ($c in $candidates) {
      if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
        $ver = Get-PythonVersion -Cmd $c
        if ($ver -eq "3.10") { return $c }
      }
    }

    $paths = @(
      (Join-Path $env:LOCALAPPDATA "Programs\\Python\\Python310\\python.exe"),
      (Join-Path $env:ProgramFiles "Python310\\python.exe"),
      (Join-Path ${env:ProgramFiles(x86)} "Python310\\python.exe")
    )
    foreach ($p in $paths) {
      if ($p -and (Test-Path $p)) { return ,@($p) }
    }

    return $null
  }

  function Install-Python310 {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
      throw "winget not found. Install App Installer or set PY_BIN to a Python 3.10 executable."
    }
    Write-Host "[start] Python 3.10 not found. Installing via winget..."
    & winget install --id Python.Python.3.10 -e --source winget --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
      throw "winget install failed with exit code $LASTEXITCODE."
    }
  }

  $found = Find-Python310
  if ($found) { return $found }

  Install-Python310

  $found = Find-Python310
  if ($found) { return $found }

  throw "Python 3.10 installed but not found. Restart the terminal or set PY_BIN to the Python 3.10 executable."
}

function Get-PythonVersion {
  param([string[]]$Cmd)
  try {
    $out = & $Cmd -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $out.Trim()
  } catch {
    return $null
  }
}

$pyCmd = Resolve-Python -UserBin $env:PY_BIN
$pyDisplay = $pyCmd -join " "
Write-Host "[start] Using Python: $pyDisplay"

$ver = Get-PythonVersion -Cmd $pyCmd
if (-not $ver) {
  throw "Unable to detect Python version."
}
if ($ver -notmatch '^3\.10$') {
  throw "Python $ver detected. This project requires Python 3.10. Set PY_BIN to Python 3.10."
}

if (-not (Test-Path ".venv")) {
  & $pyCmd -m venv .venv
}

$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  throw "Virtualenv python not found: $venvPython"
}

& $venvPython -m pip install -r requirements.txt

$BindHost = if ($env:HOST) { $env:HOST } else { "0.0.0.0" }
$BindPort = if ($env:PORT) { $env:PORT } else { "8000" }

Write-Host "[start] Serving on http://${BindHost}:$BindPort"
& $venvPython -m uvicorn app.main:app --host $BindHost --port $BindPort
