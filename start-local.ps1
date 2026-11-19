param([string]$Python = 'python', [int]$Port = 8765, [string]$SreBenchPath = $env:SRE_BENCH_PATH)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if (-not $SreBenchPath -or -not (Test-Path (Join-Path $SreBenchPath 'environment.py'))) { throw 'Pass -SreBenchPath with the upstream sre-bench directory.' }
$portProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try { $portProbe.Start() } catch { throw "Port $Port unavailable; stop the existing application first." } finally { $portProbe.Stop() }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:SRE_BENCH_PATH = (Resolve-Path $SreBenchPath).Path
$env:ENABLE_CLOUD_MODEL = 'false'
$env:LANGSMITH_TRACING = 'false'
$localDir = Join-Path $projectRoot '.local'
New-Item -ItemType Directory -Force $localDir | Out-Null
$env:DATABASE_URL = 'sqlite:///' + (Join-Path $localDir 'incident.db').Replace('\','/')
$env:CHECKPOINT_SQLITE_PATH = Join-Path $localDir 'checkpoints.db'
Push-Location $projectRoot
try {
    & $Python -m incident_simulator.manage migrate
    if ($LASTEXITCODE -ne 0) { throw 'Migration failed. Install project dependencies first.' }
    & $Python -c 'from incident_simulator import database as d; from sqlalchemy import select; s=d.Session(); print("Accounts:", len(list(s.scalars(select(d.User))))); raise SystemExit(0 if s.scalar(select(d.User.id).limit(1)) else 2)'
    if ($LASTEXITCODE -eq 2) {
        & $Python -m incident_simulator.manage create-user --username admin --role admin --credentials-file (Join-Path $localDir 'admin-account.json')
        if ($LASTEXITCODE -ne 0) { throw 'Account bootstrap failed.' }
    } elseif ($LASTEXITCODE -ne 0) { throw 'Database account check failed.' }
    $apiProcess = Start-Process -FilePath $Python -ArgumentList @('-m','uvicorn','incident_simulator.api:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $localDir 'api.log') -RedirectStandardError (Join-Path $localDir 'api-error.log')
    $workerProcess = Start-Process -FilePath $Python -ArgumentList @('-m','incident_simulator.worker') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $localDir 'worker.log') -RedirectStandardError (Join-Path $localDir 'worker-error.log')
    @{api=$apiProcess.Id; worker=$workerProcess.Id; port=$Port} | ConvertTo-Json | Set-Content (Join-Path $localDir 'processes.json')
    Write-Output "UI: http://127.0.0.1:$Port | Credentials: .local/admin-account.json"
} finally { Pop-Location }
