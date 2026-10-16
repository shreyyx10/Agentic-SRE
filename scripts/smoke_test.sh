#!/usr/bin/env bash
# End-to-end smoke test of the deployed platform, without touching the customer's cluster.
#   source ../.asre/asre.env && ./scripts/smoke_test.sh     (from the repository root)
# Names (service, topic, database, region) are read from the infra/platform
# Terraform outputs, so nothing here repeats a value that Terraform decides.
# Each run uses its own service name (smoke-<date>-<time>), so it always starts a
# new incident, never joins a real demo incident or an earlier run's, and every check
# below looks only at this run's log lines. Safe to run as often as you like.
set -euo pipefail
PROJECT="${PLATFORM:?set PLATFORM: source asre.env from your settings folder, see the README's Deploy section}"
REGION="${REGION:?set REGION: source asre.env from your settings folder, see the README's Deploy section}"
CUSTOMER="${CUSTOMER_ID:?set CUSTOMER_ID: source asre.env from your settings folder, see the README's Deploy section}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$HERE/../infra/platform"   # the repository path may contain spaces: always quoted
out() { terraform -chdir="$PLATFORM_DIR" output -json "$1"; }

INGEST_SERVICE="$(out ingest_service | python3 -c 'import json,sys;print(json.load(sys.stdin))')"
DATABASE="$(out firestore_database | python3 -c 'import json,sys;print(json.load(sys.stdin))')"
TOPIC="$(out tenant_topics | python3 -c "import json,sys;print(json.load(sys.stdin)['$CUSTOMER'])")"
SERVICE="smoke-$(date -u +%Y%m%d-%H%M%S)"
THIS_RUN="jsonPayload.service_name=\"$SERVICE\""
echo "   this run's service: $SERVICE"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
FAILED=0

logs() {  # $1 = extra filter; prints this run's matching entries from the last 10 minutes
  gcloud logging read "resource.type=\"cloud_run_revision\" AND $THIS_RUN AND $1" \
    --project "$PROJECT" --freshness=10m --limit=5 --format="${2:-value(jsonPayload.message)}"
}

echo "== 1. INGEST is not reachable from the internet"
URL="$(gcloud run services describe "$INGEST_SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/pubsub/$CUSTOMER" -d '{}' || true)"
if [[ "$CODE" == "403" || "$CODE" == "404" ]]; then pass "unauthenticated POST -> $CODE"; else fail "unauthenticated POST -> $CODE (expected 403/404)"; fi

echo "== 2. A synthetic alert becomes one group and one pending Brief"
gcloud pubsub topics publish "$TOPIC" --message="$(python3 "$HERE/synthetic_alert.py" --service "$SERVICE")" >/dev/null
echo "   published; waiting 45s for push -> ingest -> Cloud Tasks -> analyse..."
sleep 45
[[ -n "$(logs 'jsonPayload.event="alerts_received"')" ]] \
  && pass "ingest received the alert" || fail "no alerts_received log (check the push subscription and ingest logs)"
[[ -n "$(logs 'jsonPayload.event="groups_created"')" ]] \
  && pass "a new incident (alert group) was created" || fail "no groups_created log for $SERVICE"
BRIEF_ID="$(logs 'jsonPayload.event="brief_pending"' 'value(jsonPayload.brief_id)' | head -1)"
[[ -n "$BRIEF_ID" ]] \
  && pass "analyse created Brief $BRIEF_ID" || fail "no brief_pending log (check the Cloud Tasks queue and analyse logs)"

echo "== 3. A repeat inside the window is suppressed (no second analysis)"
gcloud pubsub topics publish "$TOPIC" --message="$(python3 "$HERE/synthetic_alert.py" --service "$SERVICE")" >/dev/null
sleep 30
[[ -n "$(logs 'jsonPayload.event="alert_processed" AND jsonPayload.action="suppress"')" ]] \
  && pass "duplicate suppressed" || fail "no suppress decision logged"

echo "== 4. Rejections and retries in the last 10 minutes (should be empty)"
# Not filtered to this run: a rejected push may fail before the service is known.
REJ="$(gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.event=("ingest_rejected" OR "task_rejected" OR "enqueue_failed" OR "dedup_uncertain")' \
  --project "$PROJECT" --freshness=10m --limit=5 --format='value(jsonPayload.event, jsonPayload.reason, jsonPayload.error)')"
[[ -z "$REJ" ]] && pass "no rejected pushes, failed enqueues or uncertain dedups" || { fail "found:"; echo "$REJ"; }

echo "== 5. This run's Brief is in Firestore"
if [[ -n "$BRIEF_ID" ]]; then
  TOKEN="$(gcloud auth print-access-token)"
  STATUS="$(curl -s -H "Authorization: Bearer $TOKEN" \
    "https://firestore.googleapis.com/v1/projects/$PROJECT/databases/$DATABASE/documents/customers/$CUSTOMER/briefs/$BRIEF_ID" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("fields",{}).get("status",{}).get("stringValue",""))')"
  [[ "$STATUS" == "pending" ]] && pass "Brief $BRIEF_ID stored with status pending" || fail "Brief $BRIEF_ID status: '${STATUS:-missing}'"
else
  fail "skipped: no Brief ID from step 2"
fi

exit "$FAILED"
