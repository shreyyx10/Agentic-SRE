"""Degraded mode: what a Brief says when the model cannot help.

If both models fail, or the answer is unusable, the Brief still goes out. It
carries the evidence that was gathered, the service's runbook, and these
default first steps for the alert type, written by people, not generated. No
silent failure.

Every step is read-only, so it is safe to follow without the model's reasoning.
"""

from __future__ import annotations

from asre.core.models import AlertType

DEFAULT_ACTIONS: dict[AlertType, list[str]] = {
    AlertType.OOM_KILLED: [
        "Confirm the kill: kubectl describe pod <pod> (Last State: OOMKilled) and kubectl get events.",
        "Compare memory use against the limit for the last hour in Metrics Explorer.",
        "Check for a recent limit or image change: kubectl rollout history deployment/<service>.",
        "If a recent change lowered the limit, roll it back: kubectl rollout undo deployment/<service>.",
    ],
    AlertType.CONTAINER_CRASH: [
        "Read the last crash: kubectl logs <pod> --previous.",
        "Check why it stopped: kubectl describe pod <pod> (Last State, Reason, Exit Code).",
        "Check recent deployments: kubectl rollout history deployment/<service>.",
        "If the crashes started after a deploy, roll it back: kubectl rollout undo deployment/<service>.",
    ],
    AlertType.HIGH_LATENCY: [
        "Find the slow hop: compare latency of each downstream call in Cloud Trace for the affected path.",
        "Check CPU throttling and memory of the service and its dependencies against their limits.",
        "Check recent configuration changes: kubectl rollout history for the services on the path.",
        "Check whether request volume rose sharply (load generator, campaign, bot traffic).",
    ],
    AlertType.ERROR_RATE: [
        "Read the error messages: they usually name the failing dependency.",
        "Check that every dependency has ready replicas: kubectl get deployments.",
        "Check recent deployments and scaling changes across the namespace.",
        "If one dependency is down or scaled to zero, restore it before investigating further.",
    ],
    AlertType.SATURATION: [
        "Check CPU and memory against limits for the service's pods.",
        "Check replica count and autoscaler status: kubectl get hpa, kubectl get deployment <service>.",
        "Check whether request volume rose sharply.",
    ],
    AlertType.FORECAST: [
        "Nothing has failed yet. Confirm the trend in Metrics Explorer for <service> over the last hour.",
        "Check what changed before the climb began: kubectl rollout history deployment/<service>.",
        "Check request volume and the service's dependencies for the same trend.",
        "Decide the preventive change (capacity, limit or rollback) and have a person approve it.",
    ],
    AlertType.UNKNOWN: [
        "Open the alert in Cloud Monitoring and read the condition that fired.",
        "Check the service's recent logs for errors and its recent deployments.",
    ],
}


def default_actions(alert_type: AlertType, service: str) -> list[str]:
    steps = DEFAULT_ACTIONS.get(alert_type, DEFAULT_ACTIONS[AlertType.UNKNOWN])
    return [s.replace("<service>", service) for s in steps]
