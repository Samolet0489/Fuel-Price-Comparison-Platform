# Worker metrics and the placement lab

For a provisioned local Prometheus/Grafana deployment with simulated regional
conditions and observed worker placement, use the [regional placement lab](simulation.md).
The existing single-node setup does not automatically install monitoring.

Scrape `/metrics` using `deploy/prometheus.scrape.example.yml` when integrating
with an existing Prometheus instance. Grafana uses Prometheus as a data source.
Compose starts the application services without a monitoring stack.
In Kubernetes, give the scraper permission to discover worker pods.
Scrape each pod separately rather than the load-balanced Service.

```promql
# 95th percentile whole-plan latency, seconds
histogram_quantile(0.95, sum by (le) (rate(fuel_plan_seconds_bucket[5m])))

# Plan outcomes, including routing/station/boundary unavailability
sum by (status) (rate(fuel_plans_total[5m]))

# Upstream transport/response failures (routing is now local OSRM)
sum by (provider) (rate(fuel_provider_failures_total[5m]))

# Mean upstream request duration
sum by (provider) (rate(fuel_provider_seconds_sum[5m]))
/
sum by (provider) (rate(fuel_provider_seconds_count[5m]))

# Discovered worker scrape targets
up{job="fuel-stop-workers"}
```

Boundary rejection and genuine no-route outcomes are business outcomes, not OSRM
transport failures. Stale/missing individual quotes affect plan outcomes and
ranking completeness rather than HTTP-failure counts. Rates need multiple scrapes;
label series appear after their first observation. Two-laptop monitoring has not
been verified in this change.
