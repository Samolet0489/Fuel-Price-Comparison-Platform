param([switch]$RecoveryOnly)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repo
$local = Join-Path $repo '.local/simulation'
New-Item -ItemType Directory -Force $local | Out-Null
$kind = Join-Path $repo '.local/kubernetes/kind.exe'
if (-not (Test-Path -LiteralPath $kind)) { $kind = (Get-Command kind -ErrorAction Stop).Source }
$kubeconfig = Join-Path $local 'config'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
function Invoke-Kube {
    param([string[]]$Arguments)
    Invoke-Checked kubectl (@('--kubeconfig', $kubeconfig, '--context', 'kind-fuel-stop-sim') + $Arguments)
}
foreach ($file in @('prepared.json', 'austria.osrm.mldgr', 'austria-boundary.geojson', 'stations.json')) {
    if (-not (Test-Path -LiteralPath "data/osrm/$file")) { throw "Missing prepared routing file: $file" }
}
Invoke-Checked docker @('info', '--format', '{{.OSType}}')
$clusters = & $kind get clusters
if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters' }
if ($clusters -notcontains 'fuel-stop-sim') {
    $mount = @{hostPath = (Resolve-Path 'data/osrm').Path; containerPath = '/fuel-stop-routing-data'; readOnly = $true}
    $config = @{
        kind = 'Cluster'; apiVersion = 'kind.x-k8s.io/v1alpha4'
        nodes = @(
            @{role = 'control-plane'; extraMounts = @($mount); extraPortMappings = @(
                @{containerPort = 30081; hostPort = 28001; listenAddress = '127.0.0.1'},
                @{containerPort = 30090; hostPort = 18010; listenAddress = '127.0.0.1'},
                @{containerPort = 30091; hostPort = 19090; listenAddress = '127.0.0.1'},
                @{containerPort = 30300; hostPort = 13000; listenAddress = '127.0.0.1'}
            )},
            @{role = 'worker'; extraMounts = @($mount)},
            @{role = 'worker'; extraMounts = @($mount)}
        )
    }
    $cgroupVersion = & docker info --format '{{.CgroupVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Could not check the Docker cgroup version' }
    if ($cgroupVersion -eq '1') {
        # Kubernetes 1.37 requires this compatibility override on cgroup v1.
        # It applies only to the local kind cluster.
        $config.kubeadmConfigPatches = @("apiVersion: kubelet.config.k8s.io/v1beta1`nkind: KubeletConfiguration`nfailCgroupV1: false")
    }
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $local 'kind.json') -Encoding utf8
    Invoke-Checked $kind @('create', 'cluster', '--name', 'fuel-stop-sim', '--image', 'kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5', '--config', (Join-Path $local 'kind.json'), '--kubeconfig', $kubeconfig, '--wait', '180s')
} else {
    Invoke-Checked $kind @('export', 'kubeconfig', '--name', 'fuel-stop-sim', '--kubeconfig', $kubeconfig)
}
Invoke-Kube @('label', 'node', 'fuel-stop-sim-control-plane', 'simulation.fuel-stop/role=infrastructure', '--overwrite')
Invoke-Kube @('label', 'node', 'fuel-stop-sim-worker', 'simulation.fuel-stop/role=worker', 'simulation.fuel-stop/region=at', '--overwrite')
Invoke-Kube @('label', 'node', 'fuel-stop-sim-worker2', 'simulation.fuel-stop/role=worker', 'simulation.fuel-stop/region=de', '--overwrite')
Invoke-Checked docker @('build', '-t', 'fuel-stop-worker:0.2.0', '.')
Invoke-Checked docker @('build', '-f', 'Dockerfile.jobs', '-t', 'fuel-stop-jobs:local', '.')
Invoke-Checked $kind @('load', 'docker-image', '--name', 'fuel-stop-sim', 'fuel-stop-worker:0.2.0', 'fuel-stop-jobs:local')
Invoke-Kube @('apply', '-f', 'deploy/kubernetes/base/namespace.yaml')
$secrets = @(@{name = 'fuel-stop-db'; key = 'PGPASSWORD'})
if (-not $RecoveryOnly) { $secrets += @{name = 'fuel-stop-monitoring'; key = 'admin-password'} }
foreach ($secret in $secrets) {
    $existing = & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop-sim -n fuel-stop get secret $secret.name --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Secret' }
    if (-not $existing) {
        $bytes = New-Object byte[] 32
        $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($bytes)
        } finally {
            $generator.Dispose()
        }
        $password = [Convert]::ToBase64String($bytes)
        $secretData = @{}; $secretData[$secret.key] = $password
        @{apiVersion = 'v1'; kind = 'Secret'; metadata = @{name = $secret.name; namespace = 'fuel-stop'}; type = 'Opaque'; stringData = $secretData} |
            ConvertTo-Json -Depth 5 | & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop-sim apply -f -
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Secret' }
    }
}
# Scenario changes persist across application restarts and startup reruns.
$scenario = & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop-sim -n fuel-stop get configmap fuel-stop-simulation --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect scenario' }
if (-not $scenario) { Invoke-Kube @('apply', '-f', 'deploy/kubernetes/simulation/scenario.yaml') }
$existingApplication = & kubectl --kubeconfig $kubeconfig --context kind-fuel-stop-sim -n fuel-stop get deployment fuel-stop-simulation --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect simulation deployment' }
$overlay = if ($RecoveryOnly) { 'deploy/kubernetes/recovery' } else { 'deploy/kubernetes/simulation' }
Invoke-Kube @('apply', '-k', $overlay)
if ($existingApplication) {
    Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'restart', 'deployment/fuel-stop-worker', 'deployment/fuel-stop-api', 'deployment/fuel-stop-controller', 'deployment/fuel-stop-simulation')
}
Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'status', 'statefulset/postgres', '--timeout=1200s')
$deployments = @('osrm', 'fuel-stop-api', 'fuel-stop-controller', 'fuel-stop-simulation', 'fuel-stop-worker')
if (-not $RecoveryOnly) { $deployments += @('prometheus', 'grafana') }
foreach ($deployment in $deployments) {
    Invoke-Kube @('-n', 'fuel-stop', 'rollout', 'status', "deployment/$deployment", '--timeout=1200s')
}
Invoke-Kube @('-n', 'fuel-stop', 'get', 'pods', '-o', 'wide')
Write-Host 'Controls:   http://127.0.0.1:18010/'
if (-not $RecoveryOnly) {
    Write-Host 'Grafana:    http://127.0.0.1:13000/d/fuel-placement'
    Write-Host 'Prometheus: http://127.0.0.1:19090/'
}
Write-Host 'Trip map:   http://127.0.0.1:28001/'
Write-Host 'These are two simulated countries on one physical laptop.'
