# Regional placement lab

This is a **local simulation with real Kubernetes placement**. Austria and Germany
are labels on two virtual kind worker nodes on the same physical laptop. Entered
electricity prices and carbon intensities are invented scenario inputs. They are
not live grid readings, measured emissions, cloud bills or fuel prices.

Only the planner worker relocates. The public API, trip controller, PostgreSQL,
OSRM and monitoring stay on an infrastructure node. The existing `fuel-stop`
single-node cluster on port 18001 is independent and is not recreated.

## Start

Use PowerShell 7 in the repository root with Docker Desktop running Linux
containers. Reuse the prepared Austria map and the project-local kind binary
from the [Kubernetes setup](kubernetes.md).

```powershell
./scripts/start_simulation.ps1
```

The script creates a separate cluster named `fuel-stop-sim`, labels its nodes,
mounts the existing map read-only into each virtual node, loads application
images and provisions the monitoring configuration. Public images are downloaded
on first start; cold startup can take many minutes. Three Kubernetes nodes and
monitoring need more Docker RAM and disk than the single-node setup. If resources
are tight, stop the original kind node and Compose services while using this lab.

| Page | Address |
|---|---|
| Environment controls and actual pod placement | <http://127.0.0.1:18010/> |
| Provisioned Grafana dashboard | <http://127.0.0.1:13000/d/fuel-placement> |
| Prometheus queries and scrape targets | <http://127.0.0.1:19090/> |
| Application trip map in this cluster | <http://127.0.0.1:28001/> |

All host ports bind to localhost. The control page intentionally has no login;
Grafana permits anonymous **Viewer** access. The generated Grafana administrator
password and separate PostgreSQL password live in Kubernetes Secrets. This is a
local lab, not a configuration to expose on a public or shared network.

## Classroom demonstration

1. Open the controls and Grafana side by side.
2. Select **Austria cleaner**, then **Apply conditions**. Wait for the observed
   ready worker to show Austria (`fuel-stop-sim-worker`).
3. Select **Germany cleaner**, then apply. The requested region changes first,
   followed by the observed pod moving to `fuel-stop-sim-worker2`.
4. In Grafana, watch **Actual ready worker location over time** alongside the
   simulated carbon and electricity-price charts.
5. To prove the objective matters, leave Austria cheaper and Germany cleaner,
   then change the objective between carbon intensity and electricity price.
   The preferred location changes even though the input values stay the same.
6. Untick a location's eligibility and apply to exclude it. Untick automatic
   placement to pause changes. These inputs simulate eligibility; they do not
   power off a virtual node.

Expect up to 30 seconds of cooldown plus rollout time for voluntary moves.
Conditions must improve the selected objective by at least 10%. Ties retain the
current region. Real node readiness, cordoning and scheduling taints also affect
eligibility. If the current region becomes unavailable, the controller bypasses
cooldown and selects an eligible region. If neither is eligible, it retains the
existing Deployment and reports the problem rather than deleting the worker.

Kubernetes starts a replacement pod on the selected node. The worker Deployment
uses one extra pod during rollout and keeps the old ready pod until the new one
is ready. This does not transfer an in-flight calculation or establish seamless
live process migration. Scheduling and service updates are not instantaneous.

## What the graphs mean

- **Simulated carbon / price / eligibility:** values entered in the controls.
- **Requested region:** what the placement controller asked Kubernetes to run.
- **Ready workers / placement timeline / pod identity:** observations read from
  the Kubernetes API, including actual node names. A requested location is not
  reported as an observed move before a pod is actually ready there.
- **Worker process memory and CPU:** real process metrics from each worker's
  `/metrics` endpoint, discovered and scraped independently by Prometheus.
- **Planning outcomes:** application requests; send trips to populate these.
- **Controller health / node readiness:** operational observations, not scenario
  inputs. A broken controller can leave the last placement in place.

The dashboard does not estimate total energy, monetary savings or actual CO₂
emissions. Carbon intensity alone is not a measurement of the worker's emissions.

## Verify the full flow

```powershell
uv run --frozen python -m scripts.check_simulation
```

This changes scenario inputs and creates real worker rollouts. Run it when no
important trip is in flight. It verifies carbon-driven and price-driven moves,
cross-checks the real pod/node using `kubectl`, checks paused and unavailable
scenarios, checks Prometheus scrapes and Grafana's provisioned data source, and
submits live trip jobs. It restores the previously saved scenario afterward.

```powershell
$kc = Join-Path $PWD '.local/simulation/config'
kubectl --kubeconfig $kc --context kind-fuel-stop-sim get nodes -L simulation.fuel-stop/region
kubectl --kubeconfig $kc --context kind-fuel-stop-sim -n fuel-stop get pods -o wide
kubectl --kubeconfig $kc --context kind-fuel-stop-sim -n fuel-stop logs deployment/fuel-stop-simulation --tail=50
```

The default user kubeconfig is unchanged. Simulation commands explicitly select
`.local/simulation/config` and context `kind-fuel-stop-sim`. The scenario ConfigMap
is initialized only if missing, so startup reruns preserve edited conditions.
The selected region and last switch timestamp are stored on the Deployment.

## Components and lifecycle

- `fuel_stop/placement.py`: validated scenario inputs and switching policy.
- `fuel_stop/placement_app.py`: control API, reconciliation and observed metrics.
- `web/simulation.html` and `simulation.js`: browser controls and observations.
- `deploy/kubernetes/simulation`: cluster services, scoped RBAC, Prometheus
  discovery, Grafana provisioning and dashboard.

The placement service has permission to read nodes and pods, update only the
simulation ConfigMap, and patch only the planner worker Deployment. It has no
permission to update the database, API or routing Deployments. The existing trip
controller continues dispatching jobs and is distinct from this placement
controller.

Prometheus and Grafana have PVCs. Prometheus retains up to 24 hours or 1 GB of
history. The control page's short list of recent placement requests and the
switch counter reset on placement-controller restart; the observed time series
already stored in Prometheus remain. Deleting this cluster loses its database,
scenario and monitoring history; it leaves host map files and the original
cluster intact.

To stop or resume the lab without deleting its storage:

```powershell
docker stop fuel-stop-sim-worker fuel-stop-sim-worker2 fuel-stop-sim-control-plane
docker start fuel-stop-sim-control-plane fuel-stop-sim-worker fuel-stop-sim-worker2
```

Before using real laptops or countries, replace the shared Windows-host map
mount with appropriate data distribution, make images available through a
registry, provision network access and authentication, and select a real carbon
or price data source. This demonstration does not move workloads between cloud
regions or establish machine-level failover of the whole platform.

References: [kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/),
[Prometheus Kubernetes discovery](https://prometheus.io/docs/prometheus/latest/configuration/#kubernetes_sd_config),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
