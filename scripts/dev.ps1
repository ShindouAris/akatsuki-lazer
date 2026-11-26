# Local development stack for testing with the osu! lazer client.
#   .\scripts\dev.ps1 up      containers, migrations, seed (first time), then both servers in new windows
#   .\scripts\dev.ps1 seed    (re)run the matchmaking seeder
#   .\scripts\dev.ps1 down    stop the containers
# See docs/LOCAL_CLIENT_TESTING.md.
param([Parameter(Position = 0)][ValidateSet("up", "seed", "down")][string]$Command = "up")

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"   # the log has emoji; the default Windows code page cannot print them
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$compose = @("compose", "-f", "docker-compose.yml")

function Get-EnvValue([string]$name) {
    foreach ($line in Get-Content (Join-Path $root ".env")) {
        if ($line -match "^\s*$name\s*=\s*(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return ""
}

if ($Command -eq "down") { docker @compose down; return }

if ($Command -eq "seed") { & $py -m app.seeders matchmaking; return }

foreach ($f in "dev-certs\localhost.pem", "dev-certs\localhost-key.pem", "dev-certs\ca.pem") {
    if (-not (Test-Path $f)) { throw "$f missing: run  cd multiplayer; go run ./cmd/devcert" }
}
$internalKey = Get-EnvValue "INTERNAL_API_KEY"
if (-not $internalKey) { throw "INTERNAL_API_KEY is empty in .env" }

docker @compose up -d postgres redis
for ($i = 0; $i -lt 30; $i++) {
    $state = docker inspect -f "{{.State.Health.Status}}" akatsuki-lazer-postgres-1
    if ($state -eq "healthy") { break }
    Start-Sleep -Seconds 2
}
if ($state -ne "healthy") { throw "postgres did not become healthy" }

& $py -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "migration failed" }

$pools = & $py -c "import asyncio,sqlalchemy as sa;from app.core.database import async_session_maker as m;from app.models.legacy import MatchmakingPool as P`nasync def n():`n  async with m() as s: return (await s.execute(sa.select(sa.func.count()).select_from(P))).scalar()`nprint(asyncio.run(n()))"
if ([int]$pools -eq 0) { & $py -m app.seeders matchmaking }

$ca = Join-Path $root "dev-certs\ca.pem"
$cert = Join-Path $root "dev-certs\localhost.pem"
$key = Join-Path $root "dev-certs\localhost-key.pem"
$clientId = Get-EnvValue "OAUTH_CLIENT_ID"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root'; & '$py' -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --ssl-keyfile '$key' --ssl-certfile '$cert'"

# the Go service reads its settings from the environment (inherited by the new window, so the key is not on a command line)
$env:LISTEN_ADDR = ":8081"; $env:TLS_CERT_FILE = $cert; $env:TLS_KEY_FILE = $key
$env:PYTHON_API_URL = "https://localhost:8000"; $env:API_CA_FILE = $ca; $env:INTERNAL_API_KEY = $internalKey
$env:JWT_PUBLIC_KEY_FILE = Join-Path $root "cert\public.pem"; $env:JWT_AUDIENCE = $clientId
# it refuses to start until the API answers, so give uvicorn a moment
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\multiplayer'; Start-Sleep 8; go run ./cmd/multiplayer"

Write-Host "API https://localhost:8000   multiplayer https://localhost:8081"
