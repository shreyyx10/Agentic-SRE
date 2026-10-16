"""Print a Cloud Monitoring incident (schema 1.2) for publishing to a tenant topic.

Useful before the real alert policies fire, and to try the pipeline end to end:

    source ../.asre/asre.env      # from the repository root
    gcloud pubsub topics publish "$ALERT_TOPIC" \
      --message="$(python scripts/synthetic_alert.py --service cartservice)"

The platform treats it exactly like a real notification (it arrives through the
tenant's own topic and push identity). Resource labels default to the values in
your asre.env (CUSTOMER_PROJECT, CLUSTER_NAME, K8S_NAMESPACE, ALERT_ENVIRONMENT) so the
synthetic incident looks like it came from the demo cluster.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

METRICS = {
    "container_crash": ("kubernetes.io/container/restart_count", "Container restarts (Online Boutique)"),
    "error_rate": ("logging.googleapis.com/user/frontend_5xx", "Storefront 5xx errors"),
    "high_latency": ("logging.googleapis.com/user/checkout_latency_ms", "Checkout latency p95"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="cartservice")
    ap.add_argument("--type", choices=sorted(METRICS), default="container_crash")
    ap.add_argument("--state", choices=["open", "closed"], default="open")
    ap.add_argument("--project", default=os.getenv("CUSTOMER_PROJECT", "customer-project"))
    ap.add_argument("--cluster", default=os.getenv("CLUSTER_NAME", "demo-cluster"))
    ap.add_argument("--namespace", default=os.getenv("K8S_NAMESPACE", "default"))
    ap.add_argument("--environment", default=os.getenv("ALERT_ENVIRONMENT", "prod"))
    ap.add_argument("--severity", default="critical")
    args = ap.parse_args()
    metric, policy = METRICS[args.type]
    now = int(time.time())
    print(
        json.dumps(
            {
                "version": "1.2",
                "incident": {
                    "incident_id": f"0.synthetic-{uuid.uuid4().hex[:10]}",
                    "scoping_project_id": args.project,
                    "url": "https://console.cloud.google.com/monitoring/alerting",
                    "started_at": now,
                    "ended_at": now if args.state == "closed" else None,
                    "state": args.state,
                    "summary": f"[synthetic] {policy} for {args.service}",
                    "resource": {
                        "type": "k8s_container",
                        "labels": {
                            "namespace_name": args.namespace,
                            "container_name": "server",
                            "cluster_name": args.cluster,
                        },
                    },
                    "metric": {"type": metric, "labels": {}},
                    "policy_name": policy,
                    "policy_user_labels": {
                        "service": args.service,
                        "alert_type": args.type,
                        "environment": args.environment,
                        "severity": args.severity,
                    },
                    "condition_name": "synthetic",
                },
            }
        )
    )


if __name__ == "__main__":
    main()
