"""Post an existing Brief to the Google Chat space, to check the card before an alert does.

Run it from the repository root with your own credentials (Firestore reader and
Secret Manager accessor on the platform project):

    source ../.asre/asre.env
    python scripts/post_brief.py                 # the newest Brief
    python scripts/post_brief.py brf_3dafd03...  # a specific one

It uses the same card and webhook code as ANALYSE, posts into a separate
"[preview]" thread, and changes nothing in Firestore.
"""

from __future__ import annotations

import argparse
import os
import sys

from asre.core.secrets import SecretReader
from asre.delivery.chat import WEBHOOK_SECRET, ChatDelivery, render_brief


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("brief_id", nargs="?", help="default: the most recently updated Brief")
    ap.add_argument("--customer", default=os.getenv("CUSTOMER_ID", "example-shop"))
    args = ap.parse_args()

    project = os.getenv("PLATFORM")
    if not project:
        print("set PLATFORM (source ../.asre/asre.env)", file=sys.stderr)
        return 2

    from google.cloud import firestore

    briefs = firestore.Client(project=project).collection(f"customers/{args.customer}/briefs")
    if args.brief_id:
        snap = briefs.document(args.brief_id).get()
    else:
        snap = next(iter(briefs.order_by("updated_at", direction="DESCENDING").limit(1).stream()), None)
    if snap is None or not snap.exists:
        print("no such Brief", file=sys.stderr)
        return 1
    brief = snap.to_dict()
    if not brief.get("analysis"):
        print(f"{snap.id} has no analysis yet (status {brief.get('status')})", file=sys.stderr)
        return 1

    secrets = SecretReader({WEBHOOK_SECRET: f"projects/{project}/secrets/{WEBHOOK_SECRET}"})
    message = render_brief(snap.id, brief)
    message["cardsV2"][0]["card"]["header"]["title"] = (
        "[preview] " + message["cardsV2"][0]["card"]["header"]["title"]
    )
    record = ChatDelivery(secrets).send(f"preview-{snap.id}", message)
    print(f"posted {snap.id}: {record['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
