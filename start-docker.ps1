param([string]$Proxy = 'http://127.0.0.1:7897')
$ErrorActionPreference='Stop'
$env:HTTP_PROXY=$Proxy
$env:HTTPS_PROXY=$Proxy
$env:NO_PROXY='localhost,127.0.0.1'
$docker=Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe'
Push-Location $PSScriptRoot
try { & $docker compose up -d db qdrant; if ($LASTEXITCODE -ne 0) { throw "Database/network startup failed." }; & $docker compose -p incident-observability -f compose.langfuse.yaml up -d; if ($LASTEXITCODE -ne 0) { throw "Langfuse startup failed." }; & $docker compose up --build -d; if ($LASTEXITCODE -ne 0) { throw 'Container startup failed.' } } finally { Pop-Location }
