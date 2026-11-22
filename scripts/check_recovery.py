"""Inject failures only in the local kind placement lab and restore its settings."""
import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from fuel_stop.placement import LAST_CHANGE, REGION_LABEL
from fuel_stop.placement_app import FAILED_REGION, FAILED_SCENARIO, MOVE_PENDING, PREVIOUS_REGION
from scripts.check_simulation import REPO, wait_for
from scripts.check_trip_jobs import check_trip

WORKER = "deployment/fuel-stop-worker"
CONTEXT = "kind-fuel-stop-sim"
NODES = {"at": "fuel-stop-sim-worker", "de": "fuel-stop-sim-worker2"}
ANNOTATIONS = (LAST_CHANGE, FAILED_REGION, FAILED_SCENARIO, MOVE_PENDING, PREVIOUS_REGION)
PLAN_STARTED = "RECOVERY_TEST_PLAN_STARTED"
ANTI_AFFINITY = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
    "labelSelector": {"matchLabels": {"app": "fuel-stop-worker"}},
    "topologyKey": "kubernetes.io/hostname",
}]}}


def kube(*args):
    command = ["kubectl", "--kubeconfig", str(REPO / ".local/simulation/config"),
               "--context", CONTEXT, "-n", "fuel-stop", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=20).stdout


def patch_worker(*, region, replicas, command, affinity=None, annotations=None):
    patch = {"spec": {"replicas": replicas, "template": {"spec": {
        "nodeSelector": {REGION_LABEL: region}, "affinity": affinity,
        "containers": [{"name": "worker", "command": command}],
    }}}}
    if annotations is not None:
        patch["metadata"] = {"annotations": annotations}
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "patch.json"
        path.write_text(json.dumps(patch), encoding="utf-8")
        kube("patch", WORKER, "--type=strategic", "--patch-file", str(path))


def worker_command(blocked_node="", hold=False):
    return ["python", "-c", f"""
import asyncio
import os
import uvicorn
from fuel_stop.app import app

if os.getenv("NODE_NAME") == {blocked_node!r}:
    os.environ["STATION_CATALOGUE_FILE"] = "/missing-recovery-test/stations.json"

async def test_worker(scope, receive, send):
    if {hold!r} and scope["type"] == "http" and scope["path"] == "/plan":
        print({PLAN_STARTED!r}, flush=True)
        await asyncio.sleep(15)
    await app(scope, receive, send)

uvicorn.run(test_worker, host="0.0.0.0", port=8000, limit_concurrency=4)
"""]


def ready_pods():
    pods = json.loads(kube("get", "pods", "-l", "app=fuel-stop-worker", "-o", "json"))["items"]
    return {p["metadata"]["name"]: p["spec"]["nodeName"] for p in pods
            if not p["metadata"].get("deletionTimestamp") and any(
                c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", [])
            )}


def wait_for_rollout():
    # kubectl has its own rollout timeout. Short requests keep polling if the
    # image is still starting or the API temporarily loses connectivity.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            kube("rollout", "status", WORKER, "--timeout=15s")
            return
        except subprocess.CalledProcessError:
            continue
    raise RuntimeError("Timed out waiting for the worker rollout")


def set_scenario(controls, scenario):
    controls.put("/api/scenario", json=scenario).raise_for_status()
    wait_for(controls, lambda s: s.get("scenario") == scenario, "scenario applied")


def check_rollback(controls, api, paused, region):
    other = next(r for r in NODES if r != region)
    patch_worker(region=region, replicas=1, command=worker_command(blocked_node=NODES[other]),
                 annotations={key: None for key in ANNOTATIONS})
    wait_for_rollout()

    scenario = deepcopy(paused)
    scenario.update(enabled=True, objective="carbon")
    for r in NODES:
        scenario["locations"][r].update(available=True, carbon_g_kwh=50 if r == other else 500)
    started = time.time()
    set_scenario(controls, scenario)
    target = wait_for(controls, lambda s: s["target"] == other and any(
        p["region"] == other and not p["ready"] and not p["terminating"] and p["node"] == NODES[other]
        for p in s["pods"]), f"replacement on {other} is not ready", seconds=45)
    pod = next(p["pod"] for p in target["pods"] if p["region"] == other and not p["terminating"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            logs = kube("logs", pod, "--tail=50")
            if "/health/ready HTTP/1.1\" 503" in logs:
                break
        except subprocess.CalledProcessError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError(f"Did not observe a readiness probe fail for {pod}")
    print(f"PASS: Kubernetes probed {pod} and got HTTP 503")

    wait_for(controls, lambda s: s["target"] == region and s["target_ready"] and any(
        e["time"] >= started and "restoring the previous healthy placement" in e["reason"]
        for e in s.get("events", [])), f"failed move rolled back to ready region {region}", seconds=125)
    if list(ready_pods().values()) != [NODES[region]]:
        raise RuntimeError("Kubernetes did not confirm the restored worker")
    check_trip(api, "direct-trip.json", "direct")


def check_interruption(api, cordoned):
    pods = ready_pods()
    if len(pods) != 2 or set(pods.values()) != set(NODES.values()):
        raise RuntimeError(f"Expected a ready worker on each virtual node, got {pods}")
    trip = json.loads((REPO / "examples/direct-trip.json").read_text(encoding="utf-8"))
    receipt = api.post("/trip-plans", json=trip).raise_for_status()
    if receipt.status_code != 202:
        raise RuntimeError(f"Expected a queued job, got HTTP {receipt.status_code}")
    location = receipt.headers["Location"]
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        first = api.get(location).raise_for_status().json()
        if first["status"] == "RUNNING" and first["attempts"] == 1 and first["worker_id"] in pods:
            if PLAN_STARTED in kube("logs", first["worker_id"], "--tail=30"):
                break
        time.sleep(0.25)
    else:
        raise RuntimeError("Did not observe the selected worker begin serving the trip")

    node = pods[first["worker_id"]]
    # Prevent a fresh pod from winning selection before the ready standby.
    cordoned.append(node)
    kube("cordon", node)
    kube("delete", "pod", first["worker_id"], "--grace-period=0", "--force", "--wait=false")
    print(f"Interrupted {first['worker_id']} during attempt one for job {first['id']}")

    deadline = time.monotonic() + 80
    while time.monotonic() < deadline:
        job = api.get(location).raise_for_status().json()
        if job["status"] == "FAILED":
            raise RuntimeError(f"Job {job['id']} failed after interruption: {job['error']}")
        if job["status"] == "SUCCEEDED":
            if (job["id"] != first["id"] or job["attempts"] != 2 or job["attempt_id"] == first["attempt_id"]
                    or job["worker_id"] != next(p for p in pods if p != first["worker_id"])
                    or job["worker_node"] == node or job["result"]["status"] != "direct"):
                raise RuntimeError("Retry did not complete on the original ready standby")
            print(f"PASS: same job {job['id']} succeeded on attempt two on {job['worker_id']} ({job['worker_node']})")
            return
        time.sleep(0.5)
    raise RuntimeError("Retry did not complete before the test deadline")


def main():
    # Refuse to inject faults outside the loopback kind lab.
    try:
        cluster = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        detail = (exc.stderr or "").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise SystemExit(
            "The local recovery lab is unavailable. Run .\\scripts\\start_simulation.ps1 -RecoveryOnly "
            f"and wait for it to finish before rerunning this test.\n{detail}"
        ) from None
    if (set(NODES.values()) | {"fuel-stop-sim-control-plane"}
            != {n["metadata"]["name"] for n in cluster}
            or any(n.get("spec", {}).get("unschedulable") for n in cluster if n["metadata"]["name"] in NODES.values())):
        raise RuntimeError("Expected the three-node simulation lab with both worker nodes schedulable")
    with (httpx.Client(base_url="http://127.0.0.1:18010", timeout=10) as controls,
          httpx.Client(base_url="http://127.0.0.1:28001", timeout=10) as api):
        state = wait_for(controls, lambda s: s.get("target_ready") and s["target"] in NODES, "initial placement ready")
        region = state["target"]
        original = state["scenario"]
        paused = {**original, "enabled": False}
        deployment = json.loads(kube("get", WORKER, "-o", "json"))
        spec = deployment["spec"]
        pod_spec = spec["template"]["spec"]
        if spec["replicas"] != 1 or pod_spec["nodeSelector"].get(REGION_LABEL) != region:
            raise RuntimeError("Expected the default one-worker lab with settled placement")
        original_command = next(c.get("command") for c in pod_spec["containers"] if c["name"] == "worker")
        original_annotations = deployment["metadata"].get("annotations", {})
        cordoned = []
        try:
            set_scenario(controls, paused)
            check_rollback(controls, api, paused, region)
            set_scenario(controls, paused)
            # Settle the one-replica rollout before scaling. Two new replicas
            # with hard anti-affinity cannot coexist with an old ready pod on
            # only two worker nodes.
            patch_worker(region=None, replicas=1, command=worker_command(hold=True), affinity=ANTI_AFFINITY,
                         annotations={key: None for key in ANNOTATIONS})
            wait_for_rollout()
            kube("scale", WORKER, "--replicas=2")
            wait_for_rollout()
            check_interruption(api, cordoned)
        finally:
            try:
                for node in cordoned:
                    kube("uncordon", node)
            finally:
                try:
                    patch_worker(region=region, replicas=1, command=original_command, affinity=pod_spec.get("affinity"),
                                 annotations={key: original_annotations.get(key) for key in ANNOTATIONS})
                    wait_for_rollout()
                finally:
                    controls.put("/api/scenario", json=original).raise_for_status()
    print("PASS: worker settings and scenario restored")


if __name__ == "__main__":
    main()
