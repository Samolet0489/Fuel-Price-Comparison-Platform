param(
    [string]$KindPath = "",
    [int]$Port = 18001
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo
$local = Join-Path $repo '.local/kubernetes'
New-Item -ItemType Directory -Force $local | Out-Null
if (-not $KindPath) {
    $KindPath = Join-Path $local 'kind.exe'
    if (-not (Test-Path -LiteralPath $KindPath)) {
        $KindPath = (Get-Command kind -ErrorAction Stop).Source
    }
}
$kubeconfig = Join-Path $local 'config'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
function Invoke-Kube {
    param([string[]]$Arguments)
    Invoke-Checked kubectl (@('--kubeconfig', $kubeconfig, '--context', 'kind-fuel-stop') + $Arguments)
}
foreach ($file in @('prepared.json', 'austria.osrm.mldgr', 'austria.osrm.partition', 'austria.osrm.cells', 'austria-boundary.geojson', 'stations.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $repo "data/osrm/$file"))) {
        throw "Missing data/osrm/$file. Prepare routing data using scripts.prepare_osrm first."
    }
}
Invoke-Checked docker @('info', '--format', '{{.OSType}}')
$clusters = & $KindPath get clusters
if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters' }
if ($clusters -notcontains 'fuel-stop') {
    # JSON is valid YAML and safely handles Windows paths containing spaces.
    $config = @{
        kind = 'Cluster'
        apiVersion = 'kind.x-k8s.io/v1alpha4'
        nodes = @(@{
            role = 'control-plane'
            extraMounts = @(@{
                hostPath = (Resolve-Path 'data/osrm').Path
                containerPath = '/fuel-stop-routing-data'
                readOnly = $true
            })
            extraPortMappings = @(@{
                containerPort = 30081
                hostPort = $Port
                listenAddress = '127.0.0.1'
                protocol = 'TCP'
            })
        })
    }
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $local 'kind.json') -Encoding utf8
    Invoke-Checked $KindPath @('create', 'cluster', '--name', 'fuel-stop', '--image', 'kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5', '--config', (Join-Path $local 'kind.json'), '--kubeconfig', $kubeconfig, '--wait', '180s')
} else {
    Invoke-Checked $KindPath @('export', 'kubeconfig', '--name', 'fuel-stop', '--kubeconfig', $kubeconfig)
    Write-Host 'Reusing cluster: its port and data mount are unchanged from creation.'
}
Invoke-Checked docker @('build', '-t', 'fuel-stop-worker:0.2.0', '.')
Invoke-Checked docker @('build', '-f', 'Dockerfile.jobs', '-t', 'fuel-stop-jobs:local', '.')
Invoke-Checked $KindPath @('load', 'docker-image', '--name', 'fuel-stop', 'fuel-stop-worker:0.2.0', 'fuel-stop-jobs:local')
# Public images are pulled by Kubernetes. Exporting Docker Desktop's partially
# cached multi-platform images to kind can fail on absent platform digests.
Invoke-Kube @('apply', '-f', 'deploy/kubernetes/base/namespace.yaml')
$existing = & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop -n fuel-stop get secret fuel-stop-db --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect database secret' }
if (-not $existing) {
    # Keep credentials out of source files and command-line arguments. Preserve on reruns.
    $password = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    @{
        apiVersion = 'v1'; kind = 'Secret'
        metadata = @{name = 'fuel-stop-db'; namespace = 'fuel-stop'}
        type = 'Opaque'; stringData = @{PGPASSWORD = $password}
    } | ConvertTo-Json -Depth 5 | & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Could not create database secret' }
}
$existingApplication = & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop -n fuel-stop get deployment fuel-stop-api --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect application deployment' }
Invoke-Kube @('apply', '-k', 'deploy/kubernetes/local')
# Reload locally tagged application images on repeated runs.
if ($existingApplication) {
    Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'restart', 'deployment/fuel-stop-worker', 'deployment/fuel-stop-api', 'deployment/fuel-stop-controller')
}
Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'status', 'statefulset/postgres', '--timeout=300s')
foreach ($deployment in @('osrm', 'fuel-stop-worker', 'fuel-stop-api', 'fuel-stop-controller')) {
    Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'status', "deployment/$deployment", '--timeout=300s')
}
Invoke-Kube @('-n', 'fuel-stop', 'get', 'pods,pvc,services', '-o', 'wide')
Write-Host "Kubernetes map: http://127.0.0.1:$Port/"
Write-Host 'Run ./scripts/check_kubernetes.ps1 to verify routing, worker recovery and database persistence.'
