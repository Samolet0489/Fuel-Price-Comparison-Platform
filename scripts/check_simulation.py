"""Exercise real regional placement, observations and monitoring in the demo cluster."""
import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

from scripts.check_trip_jobs import check_trip

REPO = Path(__file__).resolve().parents[1]


def wait_for(client, predicate, description, seconds=180):
    deadline = time.monotonic() + seconds
    last_reason = None
    while time.monotonic() < deadline:
        state = client.get('/api/state').raise_for_status().json()
        if state.get('healthy') and predicate(state):
            print(f'PASS: {description}')
            return state
        reason = state.get('reason')
        if reason != last_reason:
            print(f'Waiting: {reason}')
            last_reason = reason
        time.sleep(2)
    raise RuntimeError(f'Timed out: {description}')


def observed(region):
    def predicate(state):
        ready = [p for p in state.get('pods', []) if p['ready']]
        return state.get('target') == region and len(ready) == 1 and ready[0]['region'] == region
    return predicate


def actual_pod(kubeconfig, region):
    command = ['kubectl', '--kubeconfig', str(kubeconfig), '--context', 'kind-fuel-stop-sim',
               '-n', 'fuel-stop', 'get', 'pods', '-l', 'app=fuel-stop-worker', '-o', 'json']
    pods = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)['items']
    ready = [p for p in pods if not p['metadata'].get('deletionTimestamp') and any(
        c['type'] == 'Ready' and c['status'] == 'True' for c in p.get('status', {}).get('conditions', []))]
    expected = 'fuel-stop-sim-worker' if region == 'at' else 'fuel-stop-sim-worker2'
    if len(ready) != 1 or ready[0]['spec']['nodeName'] != expected:
        raise RuntimeError(f'Independent kubectl observation did not confirm {region}')
    return ready[0]['metadata']['uid']


def check_monitoring(prometheus_url, grafana_url, region):
    deadline = time.monotonic() + 45
    with httpx.Client(timeout=10) as client:
        while time.monotonic() < deadline:
            query = f'fuel_worker_ready_pods{{region="{region}"}}'
            result = client.get(f'{prometheus_url}/api/v1/query', params={'query': query}).raise_for_status().json()
            if any(float(item['value'][1]) == 1 for item in result['data']['result']):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Prometheus did not record observed placement')
        targets = client.get(f'{prometheus_url}/api/v1/targets').raise_for_status().json()['data']['activeTargets']
        for job in ('placement-simulator', 'fuel-stop-workers'):
            if not any(t['labels'].get('job') == job and t['health'] == 'up' for t in targets):
                raise RuntimeError(f'No healthy Prometheus scrape for {job}')
        client.get(f'{grafana_url}/api/health').raise_for_status()
        dashboard = client.get(f'{grafana_url}/api/dashboards/uid/fuel-placement').raise_for_status().json()
        if dashboard['dashboard']['uid'] != 'fuel-placement':
            raise RuntimeError('Provisioned Grafana dashboard missing')
        response = client.get(f'{grafana_url}/api/datasources/proxy/uid/fuel-prometheus/api/v1/query',
                              params={'query': 'fuel_placement_controller_healthy'}).raise_for_status().json()
        if not any(float(x['value'][1]) == 1 for x in response['data']['result']):
            raise RuntimeError('Grafana cannot query healthy controller metrics')
    print('PASS: Prometheus scrapes real placement and workers; Grafana dashboard queries its data source')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controls-url', default='http://127.0.0.1:18010')
    parser.add_argument('--api-url', default='http://127.0.0.1:28001')
    parser.add_argument('--prometheus-url', default='http://127.0.0.1:19090')
    parser.add_argument('--grafana-url', default='http://127.0.0.1:13000')
    parser.add_argument('--kubeconfig', type=Path, default=REPO / '.local/simulation/config')
    args = parser.parse_args()
    with httpx.Client(base_url=args.controls_url, timeout=10) as client:
        initial = wait_for(client, lambda s: bool(s.get('scenario')), 'controller healthy')
        original = initial['scenario']
        scenario = {'enabled': True, 'objective': 'carbon', 'locations': {
            'at': {'price_eur_kwh': 0.10, 'carbon_g_kwh': 60, 'available': True},
            'de': {'price_eur_kwh': 0.35, 'carbon_g_kwh': 500, 'available': True}}}
        try:
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, observed('at'), 'worker ready in simulated Austria')
            original_pod = actual_pod(args.kubeconfig, 'at')
            scenario['locations']['at']['carbon_g_kwh'] = 500
            scenario['locations']['de']['carbon_g_kwh'] = 60
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, observed('de'), 'carbon change relocated the real worker to simulated Germany')
            if actual_pod(args.kubeconfig, 'de') == original_pod:
                raise RuntimeError('Worker identity did not change during relocation')
            check_monitoring(args.prometheus_url, args.grafana_url, 'de')
            scenario['objective'] = 'price'
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, observed('at'), 'price objective relocated the worker back to Austria')
            actual_pod(args.kubeconfig, 'at')
            scenario['enabled'] = False
            scenario['locations']['at']['available'] = False
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, lambda s: not s['scenario']['enabled'], 'automatic placement paused')
            time.sleep(6)
            actual_pod(args.kubeconfig, 'at')
            print('PASS: paused controller retained placement despite simulated unavailability')
            scenario['enabled'] = True
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, observed('de'), 'unavailable Austria triggered relocation without voluntary cooldown')
            actual_pod(args.kubeconfig, 'de')
            scenario['locations']['de']['available'] = False
            client.put('/api/scenario', json=scenario).raise_for_status()
            wait_for(client, lambda s: 'No eligible location' in s['reason'], 'no eligible location safely retains the deployment')
            actual_pod(args.kubeconfig, 'de')
        finally:
            client.put('/api/scenario', json=original).raise_for_status()
        # Wait until the restored original conditions have settled before live trip checks.
        wait_for(client, lambda s: s['scenario'] == original and any(p['ready'] for p in s['pods']), 'original scenario restored')
    with httpx.Client(base_url=args.api_url, timeout=10) as client:
        check_trip(client, 'direct-trip.json', 'direct')
        check_trip(client, 'trip.json', 'prices_unavailable')
    print('PASS: simulated placement changes, actual node observations, monitoring, and live trip jobs')


if __name__ == '__main__':
    main()
