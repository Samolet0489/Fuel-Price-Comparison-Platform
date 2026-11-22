"""Local-demo controls, Kubernetes reconciliation and Prometheus observations."""
import json
import logging
import os
import ssl
import threading
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from .placement import (COOLDOWN_SECONDS, LAST_CHANGE, MIN_IMPROVEMENT,
                        REGION_LABEL, REGIONS, Scenario, choose)

logger = logging.getLogger(__name__)
NAMESPACE = os.getenv("POD_NAMESPACE", "fuel-stop")
ROOT = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/fuel-stop-worker"
CONFIG = f"/api/v1/namespaces/{NAMESPACE}/configmaps/fuel-stop-simulation"
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PREVIOUS_REGION = "simulation.fuel-stop/previous-ready-region"
MOVE_PENDING = "simulation.fuel-stop/move-pending"
FAILED_REGION = "simulation.fuel-stop/failed-region"
FAILED_SCENARIO = "simulation.fuel-stop/failed-scenario"
MOVE_TIMEOUT_SECONDS = 60


class Kubernetes:
    def __init__(self):
        self.client = httpx.Client(
            base_url="https://kubernetes.default.svc",
            verify=ssl.create_default_context(cafile=str(SA / "ca.crt")), timeout=5)

    def request(self, method, path, payload=None):
        response = self.client.request(method, path, json=payload, headers={
            "Authorization": f"Bearer {(SA / 'token').read_text().strip()}",
            "Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json"})
        response.raise_for_status()
        return response.json()

    def scenario(self):
        obj = self.request("GET", CONFIG)
        return Scenario.model_validate_json(obj["data"]["scenario.json"])

    def save(self, scenario):
        obj = self.request("GET", CONFIG)
        return self.request("PATCH", CONFIG, {
            "metadata": {"resourceVersion": obj["metadata"]["resourceVersion"]},
            "data": {"scenario.json": scenario.model_dump_json()}})


class Placement:
    def __init__(self, kube):
        self.kube = kube
        self.lock = threading.Lock()
        self.snapshot = {"healthy": False, "reason": "Starting controller", "pods": [], "nodes": []}
        self.events = []
        self.registry = CollectorRegistry()
        self.carbon = Gauge("fuel_sim_carbon_g_per_kwh", "Simulated carbon intensity, not measured emissions", ["region"], registry=self.registry)
        self.price = Gauge("fuel_sim_price_eur_per_kwh", "Simulated electricity price", ["region"], registry=self.registry)
        self.available = Gauge("fuel_sim_available", "Simulated location availability", ["region"], registry=self.registry)
        self.target = Gauge("fuel_placement_target", "Requested worker region", ["region"], registry=self.registry)
        self.running = Gauge("fuel_worker_ready_pods", "Observed ready non-terminating worker pods", ["region"], registry=self.registry)
        self.node_ready = Gauge("fuel_placement_node_ready", "Observed Kubernetes node readiness", ["node", "region"], registry=self.registry)
        self.pod_info = Gauge("fuel_worker_pod_info", "Observed worker pod identity and readiness", ["pod", "node", "region"], registry=self.registry)
        self.enabled = Gauge("fuel_placement_enabled", "Automatic placement enabled", registry=self.registry)
        self.objective = Gauge("fuel_placement_objective", "Selected simulated objective", ["objective"], registry=self.registry)
        self.healthy = Gauge("fuel_placement_controller_healthy", "Last Kubernetes reconciliation succeeded", registry=self.registry)
        self.changes = Counter("fuel_placement_changes_total", "Requested placement changes", ["region"], registry=self.registry)
        self.last_change = Gauge("fuel_placement_last_change_timestamp_seconds", "Last requested placement change", registry=self.registry)

    def reconcile(self):
        scenario = self.kube.scenario()
        deployment = self.kube.request("GET", ROOT)
        nodes = self.kube.request("GET", "/api/v1/nodes")["items"]
        pods = self.kube.request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3Dfuel-stop-worker")["items"]
        node_regions, ready_regions, observations = {}, set(), []
        self.node_ready.clear()
        for node in nodes:
            region = node["metadata"].get("labels", {}).get(REGION_LABEL)
            if region not in REGIONS:
                continue
            name = node["metadata"]["name"]
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", []))
            ready = ready and not node.get("spec", {}).get("unschedulable", False)
            # Do not choose a node with an untolerated scheduling taint.
            ready = ready and not any(t["effect"] in ("NoSchedule", "NoExecute") for t in node.get("spec", {}).get("taints", []))
            node_regions[name] = region
            observations.append({"node": name, "region": region, "ready": ready})
            self.node_ready.labels(name, region).set(ready)
            if ready:
                ready_regions.add(region)
        counts = dict.fromkeys(REGIONS, 0)
        pod_observations = []
        self.pod_info.clear()
        for pod in pods:
            node = pod.get("spec", {}).get("nodeName", "pending")
            region = node_regions.get(node, "unassigned")
            terminating = bool(pod["metadata"].get("deletionTimestamp"))
            ready = not terminating and any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))
            name = pod["metadata"]["name"]
            pod_observations.append({"pod": name, "node": node, "region": region, "ready": ready, "terminating": terminating})
            self.pod_info.labels(name, node, region).set(ready)
            if ready and region in counts:
                counts[region] += 1
        current = deployment["spec"]["template"]["spec"].get("nodeSelector", {}).get(REGION_LABEL)
        annotations = deployment["metadata"].get("annotations", {})
        last = float(annotations.get(LAST_CHANGE, "0"))
        now = time.time()
        scenario_id = sha256(scenario.model_dump_json().encode()).hexdigest()
        failed_region = annotations.get(FAILED_REGION) if annotations.get(FAILED_SCENARIO) == scenario_id else None
        decision = choose(scenario, current, ready_regions - {failed_region}, last, now)
        desired = deployment["spec"].get("replicas", 1)
        status = deployment.get("status", {})
        observed = status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        settled = (observed
                   and status.get("updatedReplicas", 0) == desired
                   and status.get("replicas", 0) == desired
                   and counts.get(current, 0) == desired)
        previous = annotations.get(PREVIOUS_REGION)
        environments = scenario.locations.model_dump()
        deadline_exceeded = observed and any(
            c.get("type") == "Progressing" and c.get("status") == "False"
            and c.get("reason") == "ProgressDeadlineExceeded" for c in status.get("conditions", [])
        )
        pending = annotations.get(MOVE_PENDING) == current and current in REGIONS
        failed = not settled and (deadline_exceeded or (pending and now - last >= MOVE_TIMEOUT_SECONDS))
        reason = decision.reason
        target = None
        changes = {}
        if failed:
            # The annotation survives a controller restart. Older deployments
            # may still have the original ready pod during a failed rollout.
            if previous not in REGIONS and not annotations.get(FAILED_REGION):
                previous = next((region for region in REGIONS if region != current and counts[region]), None)
            if previous in ready_regions and previous != current and environments[previous]["available"]:
                target = previous
                changes = {PREVIOUS_REGION: None, FAILED_REGION: current, FAILED_SCENARIO: scenario_id}
                reason = "Worker rollout failed; restoring the previous healthy placement"
            else:
                reason = "Worker rollout failed; no previous eligible placement to restore"
        elif failed_region and current != failed_region and not settled:
            reason = "Waiting for the restored worker to become ready"
        elif decision.target and decision.target != current:
            unavailable = current not in ready_regions or not environments.get(current, {}).get("available", False)
            if settled or unavailable:
                target = decision.target
                changes = {PREVIOUS_REGION: current if counts.get(current) else None,
                           FAILED_REGION: None, FAILED_SCENARIO: None}
            else:
                reason = "Waiting for the previous worker rollout to finish"
        elif failed_region and settled:
            reason = "Previous placement is ready; change the scenario to retry the failed region"
        if target:
            self.kube.request("PATCH", ROOT, {
                "metadata": {"resourceVersion": deployment["metadata"]["resourceVersion"],
                             "annotations": {LAST_CHANGE: str(now), MOVE_PENDING: target, **changes}},
                "spec": {"template": {"spec": {"nodeSelector": {REGION_LABEL: target}}}}})
            current, last = target, now
            settled = False
            self.changes.labels(current).inc()
            with self.lock:
                self.events = ([{"time": now, "target": current, "reason": reason}] + self.events)[:30]
        elif settled and pending:
            self.kube.request("PATCH", ROOT, {"metadata": {
                "resourceVersion": deployment["metadata"]["resourceVersion"],
                "annotations": {MOVE_PENDING: None, PREVIOUS_REGION: None},
            }})
        for region, environment in environments.items():
            self.carbon.labels(region).set(environment["carbon_g_kwh"])
            self.price.labels(region).set(environment["price_eur_kwh"])
            self.available.labels(region).set(environment["available"])
            self.target.labels(region).set(current == region)
            self.running.labels(region).set(counts[region])
        self.enabled.set(scenario.enabled)
        for objective in ("carbon", "price"):
            self.objective.labels(objective).set(scenario.objective == objective)
        self.last_change.set(last)
        self.healthy.set(1)
        with self.lock:
            self.snapshot = {"healthy": True, "scenario": scenario.model_dump(), "target": current,
                             "target_ready": settled,
                             "reason": reason, "wait_seconds": decision.wait_seconds,
                             "last_change": last, "nodes": observations, "pods": pod_observations,
                             "observed_at": now, "cooldown_seconds": COOLDOWN_SECONDS,
                             "minimum_improvement_percent": MIN_IMPROVEMENT * 100}

    def run(self, stop):
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:
                logger.warning("Placement reconciliation failed (%s)", type(exc).__name__)
                self.healthy.set(0)
                with self.lock:
                    self.snapshot = {**self.snapshot, "healthy": False, "reason": "Cannot reconcile Kubernetes state; no new placement requested"}
            stop.wait(2)


@asynccontextmanager
async def lifespan(app):
    kube = Kubernetes()
    app.state.placement = Placement(kube)
    stop = threading.Event()
    thread = threading.Thread(target=app.state.placement.run, args=(stop,), daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=10)
    kube.client.close()


app = FastAPI(title="Simulated regional placement", lifespan=lifespan)
web = Path(__file__).resolve().parents[1] / "web"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(web / "simulation.html")


@app.get("/simulation.js", include_in_schema=False)
def script():
    return FileResponse(web / "simulation.js", media_type="text/javascript")


@app.get("/api/state")
def state():
    placement = app.state.placement
    with placement.lock:
        return {**placement.snapshot, "events": list(placement.events)}


@app.put("/api/scenario")
def update(scenario: Scenario, request: Request):
    # The demo binds only to localhost. Also reject browser cross-origin writes.
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Cross-origin scenario changes are not allowed")
    try:
        app.state.placement.kube.save(scenario)
    except httpx.HTTPError:
        raise HTTPException(503, "Could not save scenario; retry after checking controller health") from None
    return {"saved": True, "scenario": scenario.model_dump()}


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    if not app.state.placement.snapshot["healthy"]:
        raise HTTPException(503, "Waiting for successful Kubernetes reconciliation")
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(app.state.placement.registry), media_type=CONTENT_TYPE_LATEST)
