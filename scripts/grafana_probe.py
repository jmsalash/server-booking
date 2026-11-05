#!/usr/bin/env python
"""Read-only probe of the lab Grafana: lists datasources and metric label sets.

Usage: python scripts/grafana_probe.py --url https://grafana.lab --user me --password ...
       python scripts/grafana_probe.py --url https://grafana.lab --token glsa_...
"""
import argparse
import json
import sys

import httpx

METRICS = ["up", "node_cpu_seconds_total", "DCGM_FI_DEV_GPU_UTIL", "nvidia_smi_utilization_gpu_ratio"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--token")
    a = p.parse_args()
    headers, auth = {}, None
    if a.token:
        headers["Authorization"] = f"Bearer {a.token}"
    elif a.user:
        auth = (a.user, a.password or "")
    else:
        p.error("give --token or --user/--password")
    with httpx.Client(base_url=a.url.rstrip("/"), headers=headers, auth=auth, timeout=15) as c:
        r = c.get("/api/datasources")
        r.raise_for_status()
        dss = r.json()
        print("Datasources:")
        for d in dss:
            print(f"  uid={d['uid']} name={d['name']} type={d['type']}")
        prom = next((d for d in dss if d["type"] == "prometheus"), None)
        if prom is None:
            print("NO PROMETHEUS DATASOURCE FOUND - stop and revise Task 9", file=sys.stderr)
            return 2
        print(f"\nUsing Prometheus datasource {prom['name']} ({prom['uid']})")
        for m in METRICS:
            body = {
                "queries": [{"refId": "A", "datasource": {"uid": prom["uid"]}, "expr": m, "instant": True}],
                "from": "now-5m",
                "to": "now",
            }
            r = c.post("/api/ds/query", json=body)
            print(f"\n== {m} (HTTP {r.status_code})")
            if r.status_code != 200:
                print(r.text[:300])
                continue
            frames = r.json().get("results", {}).get("A", {}).get("frames", [])
            print(f"{len(frames)} series")
            for f in frames[:20]:
                fields = f["schema"]["fields"]
                labels = fields[1].get("labels", {}) if len(fields) > 1 else {}
                print("  ", json.dumps(labels, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
