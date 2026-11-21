param([string]$ApiUrl = 'http://127.0.0.1:18001')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo
$kubeconfig = Join-Path $repo '.local/kubernetes/config'
function Invoke-Kube {
    param([string[]]$Arguments)
    & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop -n fuel-stop @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'kubectl failed' }
}
function Wait-ApiReady {
    $readyDeadline = (Get-Date).AddSeconds(60)
    do {
        try {
            $health = Invoke-RestMethod "$ApiUrl/health/ready" -TimeoutSec 3
            if ($health.status -eq 'ok') { return }
        } catch {
            # Service endpoints can lag pod readiness briefly after replacement.
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $readyDeadline)
    throw 'Public API did not regain database connectivity within 60 seconds'
}
Wait-ApiReady
& uv run --frozen python -m scripts.check_trip_jobs --api-url $ApiUrl
if ($LASTEXITCODE -ne 0) { throw 'Live trip-job verification failed' }
$page = Invoke-WebRequest "$ApiUrl/" -TimeoutSec 15
if ($page.StatusCode -ne 200 -or $page.Content -notmatch 'FuelRoute') { throw 'Map page not served' }
$catalogue = Invoke-RestMethod "$ApiUrl/stations" -TimeoutSec 15
if ($catalogue.stations.Count -lt 1) { throw 'Station catalogue is empty' }
Write-Host "Map served; catalogue contains $($catalogue.stations.Count) stations."

# Keep one completed job to verify after replacing the PostgreSQL pod.
$body = Get-Content examples/direct-trip.json -Raw
$receipt = Invoke-RestMethod "$ApiUrl/trip-plans" -Method Post -ContentType application/json -Body $body
$deadline = (Get-Date).AddSeconds(60)
do {
    $job = Invoke-RestMethod "$ApiUrl/trip-plans/$($receipt.id)"
    if ($job.status -eq 'FAILED') { throw 'Persistence test job failed' }
    if ($job.status -eq 'SUCCEEDED') { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
if ($job.status -ne 'SUCCEEDED') { throw 'Persistence test job did not complete' }
Invoke-Kube @('rollout', 'restart', 'deployment/fuel-stop-worker')
Invoke-Kube @('rollout', 'status', 'deployment/fuel-stop-worker', '--timeout=180s')
Invoke-Kube @('rollout', 'restart', 'statefulset/postgres')
Invoke-Kube @('rollout', 'status', 'statefulset/postgres', '--timeout=180s')
Wait-ApiReady
$saved = Invoke-RestMethod "$ApiUrl/trip-plans/$($receipt.id)" -TimeoutSec 15
if ($saved.status -ne 'SUCCEEDED' -or $saved.result.route.distance_km -ne $job.result.route.distance_km) {
    throw 'Completed job was not preserved after PostgreSQL restart'
}
& uv run --frozen python -m scripts.check_trip_jobs --api-url $ApiUrl
if ($LASTEXITCODE -ne 0) { throw 'Trip verification failed after pod replacements' }
Write-Host "PASS: live trips, web/catalogue, worker replacement and persisted job $($receipt.id)."
