# Local Kubernetes milestone

This deployment runs the existing application in an isolated, single-node kind
cluster on Docker Desktop. It includes the public API/web map, controller,
PostgreSQL, one planner worker and OSRM. Compose remains available on port 8001;
Kubernetes uses port **18001**. No application logic changes are required.

This is a development deployment. Two physical laptops, carbon-aware placement,
cluster failover, monitoring installation and an authorized price feed are later
milestones. Two worker replicas alone do not make PostgreSQL or OSRM highly available.

Verified on 21 November 2026: live direct/low-fuel jobs, served HTML, 465 catalogue
stations, worker replacement and retained job results after PostgreSQL pod
replacement. See the [verification record](verification.md) for measured results.

## Prerequisites

- PowerShell 7, Docker Desktop using Linux containers, `kubectl`, and `uv`.
- Existing prepared `data/osrm` from the Compose setup, including `prepared.json`,
  OSRM MLD graph files, `austria-boundary.geojson`, and `stations.json`.
- Enough Docker memory for Kubernetes plus OSRM and any running Compose services.
  OSRM has a provisional 2 GiB memory limit; inspect OOM events if a larger map is used.
- kind v0.33.0. If it is not on PATH, download a project-local copy from the
  official release and verify its checksum:

```powershell
New-Item -ItemType Directory -Force .local/kubernetes | Out-Null
Invoke-WebRequest https://kind.sigs.k8s.io/dl/v0.33.0/kind-windows-amd64 -OutFile .local/kubernetes/kind.exe
Invoke-WebRequest https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-windows-amd64.sha256sum -OutFile .local/kubernetes/kind.sha256sum
$expected = ((Get-Content .local/kubernetes/kind.sha256sum -Raw).Trim() -split '\s+')[0]
if ((Get-FileHash .local/kubernetes/kind.exe -Algorithm SHA256).Hash -ne $expected) { throw 'kind checksum mismatch' }
```

## Start and verify

From the repository root:

```powershell
./scripts/start_kubernetes.ps1
./scripts/check_kubernetes.ps1
```

Open <http://127.0.0.1:18001/> for the map and
<http://127.0.0.1:18001/docs> for API documentation. The default low-fuel trip
uses generated station prices and the direct example returns `direct`.
Map tiles and Leaflet assets
require internet access, while routing and station lookup use local data.

The startup script builds application images, loads them into kind, creates a
random database Secret if absent, applies the local overlay and waits for
rollouts. On subsequent runs it preserves the existing Secret/database, reloads
images and restarts application Deployments. It never changes your default
kubeconfig: all cluster commands explicitly select the ignored
`.local/kubernetes/config` and context `kind-fuel-stop`.

The check script submits live direct/low-fuel jobs, checks the served map and
catalogue, replaces the worker and PostgreSQL pods, verifies a completed job
survives, and submits new jobs after replacement. Run it when no important job
is in flight: it intentionally restarts those pods.

## Inspect

```powershell
$kc = Join-Path $PWD '.local/kubernetes/config'
kubectl --kubeconfig $kc --context kind-fuel-stop -n fuel-stop get pods,pvc,services -o wide
kubectl --kubeconfig $kc --context kind-fuel-stop -n fuel-stop logs deployment/fuel-stop-controller --tail=50
kubectl --kubeconfig $kc --context kind-fuel-stop -n fuel-stop get events --sort-by=.lastTimestamp
```

For direct worker diagnostics, run the following in another terminal and use
<http://127.0.0.1:18000/docs> or `/health/live` (includes pod/node identity):

```powershell
kubectl --kubeconfig .local/kubernetes/config --context kind-fuel-stop -n fuel-stop port-forward service/fuel-stop-worker 18000:8000
```

## Storage and lifecycle

The startup script creates a kind node with an extra read-only mount from the
repository's `data/osrm` to `/fuel-stop-routing-data`. The local overlay binds a
static PV/PVC to that node path for OSRM and the worker. This single-node hostPath
does not provide data sharing between laptops. Stop Kubernetes workloads before
replacing prepared files; graph and OSRM image versions must match.

PostgreSQL uses a separate PVC provisioned by kind's default storage class. It
survives pod replacement and is independent of Compose's database. It is **not
backed up**: deleting the cluster deletes its Kubernetes database storage. Map
files remain on the Windows host. Do not delete the Secret while retaining an
initialized database: generating a new password does not rotate PostgreSQL's
existing password.

To stop this development cluster temporarily:

```powershell
docker stop fuel-stop-control-plane
# Resume it:
docker start fuel-stop-control-plane
```

To deliberately destroy the cluster and its Kubernetes database:

```powershell
./.local/kubernetes/kind.exe delete cluster --name fuel-stop
```

The API port and routing-data host path are fixed when the cluster is created.
Passing a different `-Port` on a later startup does not reconfigure an existing
cluster. Back up needed jobs before recreating it.

## Manifest layout and next milestone

- `deploy/kubernetes/base`: namespace, worker, OSRM and job services. The base
  retains the original two-worker cross-node anti-affinity intent and expects
  routing storage plus a database Secret supplied by the installation.
- `deploy/kubernetes/local`: one worker, no cross-node anti-affinity, local routing
  PV/PVC, and API NodePort mapped to Windows localhost by kind.

For two laptops, create a separate K3s overlay: publish application images to a
registry, replace `imagePullPolicy: Never` and local tags, provision routing data
on the appropriate nodes or shared storage, choose persistent database storage,
and verify both node identities. Do not apply the local hostPath configuration
unchanged to that cluster. Carbon-aware dispatch still needs application work.

References: [kind quick start](https://kind.sigs.k8s.io/docs/user/quick-start/),
[kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/),
[Kubernetes single-node storage example](https://kubernetes.io/docs/tutorials/configuration/configure-persistent-volume-storage/).
