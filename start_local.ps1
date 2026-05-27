$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

function Test-PortListening {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$listener
}

function Test-PortInUse {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    return [bool]$connections
}

function Wait-PortState {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [bool]$ShouldBeListening,
        [int]$TimeoutSeconds = 15
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Test-PortListening -Port $Port) -eq $ShouldBeListening) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }

    return ((Test-PortListening -Port $Port) -eq $ShouldBeListening)
}

function Wait-PortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-PortInUse -Port $Port)) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }

    return (-not (Test-PortInUse -Port $Port))
}

function Stop-PortListeners {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Ports
    )

    foreach ($port in $Ports) {
        $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($listeners) {
            $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($procId in $pids) {
                try {
                    Stop-Process -Id $procId -Force -ErrorAction Stop
                    Wait-Process -Id $procId -Timeout 5 -ErrorAction SilentlyContinue
                    Write-Host "Stopped process $procId on port $port"
                } catch {
                    Write-Warning "Failed to stop process $procId on port ${port}: $($_.Exception.Message)"
                }
            }
        }

        if (-not (Wait-PortAvailable -Port $port -TimeoutSeconds 20)) {
            Write-Warning "Port $port is still in use after stop attempts."
        }
    }
}

Stop-PortListeners -Ports @(4500, 8080)

uv sync

$runtimeDir = Join-Path $PSScriptRoot "runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$codexOut = Join-Path $runtimeDir "codex_app_server.$stamp.out.log"
$codexErr = Join-Path $runtimeDir "codex_app_server.$stamp.err.log"
$webOut = Join-Path $runtimeDir "web_client.$stamp.out.log"
$webErr = Join-Path $runtimeDir "web_client.$stamp.err.log"

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
$codexBinary = $null

if ($codexCommand -and $codexCommand.Source -match '\.exe$') {
    $codexBinary = $codexCommand.Source
} else {
    $codexCmd = Get-Command codex.cmd -ErrorAction SilentlyContinue
    if ($codexCmd) {
        $codexBinary = $codexCmd.Source
    }
}

if (-not $codexBinary) {
    throw "Cannot find executable codex binary (codex.exe/codex.cmd) in PATH."
}

$codexProc = Start-Process `
    -FilePath $codexBinary `
    -ArgumentList @("app-server", "--listen", "ws://127.0.0.1:4500") `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $codexOut `
    -RedirectStandardError $codexErr

$webProc = Start-Process `
    -FilePath "uv" `
    -ArgumentList @("run", "waitress-serve", "--host=0.0.0.0", "--port=8080", "app:app") `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $webOut `
    -RedirectStandardError $webErr

$listen4500 = Wait-PortState -Port 4500 -ShouldBeListening $true -TimeoutSeconds 15
$listen8080 = Wait-PortState -Port 8080 -ShouldBeListening $true -TimeoutSeconds 15

if (-not $listen4500 -and $codexProc.HasExited) {
    Write-Warning "codex process exited early; see log: $codexErr"
}

if (-not $listen8080 -and $webProc.HasExited) {
    Write-Warning "web process exited early; see log: $webErr"
}

Write-Host ""
Write-Host "codex pid: $($codexProc.Id)"
Write-Host "web   pid: $($webProc.Id)"
Write-Host "4500 listening: $listen4500"
Write-Host "8080 listening: $listen8080"
Write-Host ""
Write-Host "Open: http://127.0.0.1:8080"
Write-Host "Logs:"
Write-Host "  $codexOut"
Write-Host "  $codexErr"
Write-Host "  $webOut"
Write-Host "  $webErr"
