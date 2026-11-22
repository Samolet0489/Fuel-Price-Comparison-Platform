import pytest
from pydantic import ValidationError

from fuel_stop.placement import Scenario, choose


def test_objectives_choose_different_regions():
    scenario = Scenario()
    assert choose(scenario, 'de', {'at', 'de'}, 0, 100).target == 'at'
    scenario.objective = 'price'
    assert choose(scenario, 'at', {'at', 'de'}, 0, 100).target == 'de'


def test_cooldown_then_switch_and_threshold_prevents_flapping():
    scenario = Scenario()
    decision = choose(scenario, 'de', {'at', 'de'}, 90, 100)
    assert decision.target == 'de' and decision.wait_seconds == 20
    assert choose(scenario, 'de', {'at', 'de'}, 90, 121).target == 'at'
    scenario.locations.at.carbon_g_kwh = 390
    assert choose(scenario, 'de', {'at', 'de'}, 0, 100).target == 'de'


def test_tie_and_zero_preserve_current():
    scenario = Scenario()
    scenario.locations.at.carbon_g_kwh = scenario.locations.de.carbon_g_kwh = 0
    assert choose(scenario, 'de', {'at', 'de'}, 0, 100).target == 'de'


def test_unavailable_location_bypasses_cooldown_but_no_eligible_keeps_deployment():
    scenario = Scenario()
    scenario.locations.at.available = False
    assert choose(scenario, 'at', {'at', 'de'}, 99, 100).target == 'de'
    assert choose(scenario, 'at', {'at'}, 0, 100).target is None
    assert choose(scenario, 'de', {'at'}, 0, 100).target is None


def test_real_node_readiness_overrides_simulated_conditions_and_pause():
    scenario = Scenario()
    assert choose(scenario, 'at', {'de'}, 99, 100).target == 'de'
    scenario.enabled = False
    assert choose(scenario, 'at', {'de'}, 0, 100).target is None


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), 2001])
def test_invalid_simulated_carbon_is_rejected(value):
    payload = Scenario().model_dump()
    payload['locations']['at']['carbon_g_kwh'] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate(payload)


class FakeKubernetes:
    def __init__(self):
        from fuel_stop.placement import REGION_LABEL
        self.configuration = Scenario()
        self.patches = []
        self.deployment = {
            'metadata': {'resourceVersion': '7', 'generation': 2},
            'spec': {'replicas': 1, 'template': {'spec': {'nodeSelector': {REGION_LABEL: 'at'}}}},
            'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'replicas': 1}}
        self.nodes = [{'metadata': {'name': r, 'labels': {REGION_LABEL: r}},
                       'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                      for r in ('at', 'de')]
        self.pods = [{'metadata': {'name': 'worker-at'}, 'spec': {'nodeName': 'at'},
                      'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]

    def scenario(self):
        return self.configuration

    def request(self, method, path, payload=None):
        if method == 'PATCH':
            self.patches.append((path, payload))
            return {}
        if path.endswith('/nodes'):
            return {'items': self.nodes}
        if '/pods?' in path:
            return {'items': self.pods}
        return self.deployment


def test_reconciler_requests_new_region_without_fabricating_observed_placement():
    from fuel_stop.placement_app import Placement
    from fuel_stop.placement import REGION_LABEL
    kube = FakeKubernetes()
    kube.configuration.objective = 'price'
    placement = Placement(kube)
    placement.reconcile()
    assert placement.snapshot['target'] == 'de'
    assert placement.snapshot['pods'][0]['region'] == 'at'
    patch = kube.patches[0][1]
    assert patch['metadata']['resourceVersion'] == '7'
    assert patch['spec']['template']['spec']['nodeSelector'] == {REGION_LABEL: 'de'}


def test_reconciler_does_not_interrupt_an_unfinished_voluntary_rollout():
    from fuel_stop.placement_app import Placement
    kube = FakeKubernetes()
    kube.configuration.objective = 'price'
    kube.deployment['status']['replicas'] = 2
    placement = Placement(kube)
    placement.reconcile()
    assert not kube.patches
    assert placement.snapshot['target'] == 'at'
    assert 'rollout' in placement.snapshot['reason']


@pytest.mark.parametrize("failure", ["progress_deadline", "controller_timeout"])
def test_failed_rollout_restores_previous_placement_and_waits_for_readiness(monkeypatch, failure):
    from fuel_stop.placement import LAST_CHANGE, REGION_LABEL
    from fuel_stop.placement_app import FAILED_REGION, MOVE_PENDING, PREVIOUS_REGION, Placement

    kube = FakeKubernetes()
    kube.configuration.objective = "price"
    original = kube.deployment
    original["spec"]["template"]["spec"]["nodeSelector"][REGION_LABEL] = "de"
    original["metadata"]["annotations"] = {LAST_CHANGE: "90", PREVIOUS_REGION: "at", MOVE_PENDING: "de"}
    original["status"].update(updatedReplicas=1, replicas=2)
    stamp = 100 if failure == "progress_deadline" else 151
    if failure == "progress_deadline":
        original["status"]["conditions"] = [
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"},
        ]
    monkeypatch.setattr("fuel_stop.placement_app.time.time", lambda: stamp)
    placement = Placement(kube)
    placement.reconcile()
    assert placement.snapshot["target"] == "at"
    assert not placement.snapshot["target_ready"]
    _, rollback = kube.patches.pop()
    assert rollback["spec"]["template"]["spec"]["nodeSelector"][REGION_LABEL] == "at"
    assert rollback["metadata"]["annotations"][FAILED_REGION] == "de"

    original["metadata"].update(generation=3, annotations=rollback["metadata"]["annotations"])
    original["spec"]["template"]["spec"]["nodeSelector"][REGION_LABEL] = "at"
    original["status"]["conditions"] = []
    placement.reconcile()
    assert not placement.snapshot["target_ready"]
    assert "restored worker" in placement.snapshot["reason"]
    assert not kube.patches

    original["status"].update(observedGeneration=3, updatedReplicas=1, replicas=1)
    stamp += 45
    placement.reconcile()
    assert placement.snapshot["target_ready"]
    assert "Previous placement is ready" in placement.snapshot["reason"]
    assert len(kube.patches) == 1
    assert "spec" not in kube.patches[0][1]


def test_reconciler_excludes_tainted_nodes_and_terminating_pods():
    from fuel_stop.placement_app import Placement
    kube = FakeKubernetes()
    kube.nodes[0]['spec'] = {'taints': [{'effect': 'NoSchedule'}]}
    kube.pods[0]['metadata']['deletionTimestamp'] = '2026-11-21T00:00:00Z'
    placement = Placement(kube)
    placement.reconcile()
    assert placement.snapshot['target'] == 'de'
    assert not placement.snapshot['pods'][0]['ready']
    assert not placement.snapshot['nodes'][0]['ready']


def test_scenario_api_rejects_cross_origin_writes_and_invalid_values():
    from fastapi.testclient import TestClient
    from fuel_stop.placement_app import app, Placement
    kube = FakeKubernetes()
    saved = []
    kube.save = saved.append
    app.state.placement = Placement(kube)
    client = TestClient(app)
    payload = Scenario().model_dump()
    assert client.put('/api/scenario', json=payload, headers={'Origin': 'http://unrelated.example'}).status_code == 403
    assert not saved
    assert client.put('/api/scenario', json=payload, headers={'Origin': 'http://testserver'}).status_code == 200
    payload['locations']['at']['price_eur_kwh'] = -1
    assert client.put('/api/scenario', json=payload).status_code == 422
