# ADR-0002 — Model platform: Gemini on Vertex AI

- **Status**: Accepted — 10 Oct 2026 by the engineering lead: "Yes, let's change that to 3.1 pro". Recommended 9 Oct by Shreyansh Srivastava, verified against the platform project, implemented as configuration (`infra/platform` `models`, `asre/agent/model.py`).
- **Deciders**: Engineering Lead

## Context
AI Builder Cup rules require Google AI models (Gemini or Gemma) or Google's agent platform. The earlier design chose Claude on Vertex, mainly because Vertex uses IAM instead of an API key.

ADR-0001 already settles *where* the model runs. It says "Gemini is reached through the Vertex AI **global endpoint**" because "region does not constrain model access". Data at rest (Firestore, logs and Briefs) and every service stay in `australia-southeast1`. This ADR picks *which* models to use, choosing the best Pro and Flash models while keeping cost low.

What changed since this ADR was opened (checked 9 Oct 2026):

- **Gemini 2.5 Pro, Flash and Flash-Lite retire "no earlier than 16 October 2026"** on Vertex AI. That date is a floor, not a firm date, so building on 2.5 risks the model disappearing during judging (17 Oct to 6 Nov, or 4 Dec if shortlisted).
- **Google's recommended replacements are Gemini 3.x.** The only Pro-class 3.x model is `gemini-3.1-pro-preview`. The newest Flash is `gemini-3.8-flash`.
- **Most Gemini 3.x models are served only on the global endpoint.** Of the models tested, only 3.5 Flash is also served in Sydney (see the table below).

## Verification and price (platform project, 9 Oct 2026)
**Served** is the result of a live "Reply with OK" call: ✅ means HTTP 200, and ✗ means `404 Publisher model … not found`. **Price** is the Vertex AI list price on the global endpoint, in USD per 1M tokens (input / output, prompts of 200K tokens or less). Output includes thinking tokens.

| Model | Class | `global` | `australia-southeast1` | Price (in / out) |
|---|---|---|---|---|
| **gemini-3.1-pro-preview** | Pro (preview) | ✅ | ✗ | $2.00 / $12.00 |
| gemini-2.5-pro | Pro (retiring) | ✅ | ✗ | $1.25 / $10.00 |
| **gemini-3.8-flash** | Flash | ✅ | ✗ | $0.75 / $3.75 until 31 Dec 2026, then $1.50 / $7.50 |
| gemini-3.5-flash | Flash | ✅ | ✅ | $1.50 / $9.00 (Sydney: $1.65 / $9.90) |
| gemini-2.5-flash | Flash (retiring) | ✅ | ✅ | $0.30 / $2.50 |
| gemini-3.5-flash-lite | Flash-Lite | ✅ | ✗ | $0.30 / $2.50 |
| gemini-3.1-flash-lite | Flash-Lite | ✅ | ✗ | $0.25 / $1.50 |
| gemini-3.1-pro, gemini-3-flash | — | ✗ | ✗ | not served under these IDs |

Two findings from the table:

- **3.8 Flash beats 3.5 Flash on both counts.** It is newer and costs about half as much (and still less after the introductory price ends).
- **The Sydney option costs more for less.** 3.5 Flash in Sydney is the most expensive Flash and weaker than 3.8 Flash. It is only worth choosing for the residency story.

## Options
- **A. Pro for the hypothesis, Flash for the critic, both on `global` (recommended).** The hypothesis uses `gemini-3.1-pro-preview` and the critic uses `gemini-3.8-flash`. The step that needs the deepest reasoning gets Pro. Each incident calls Pro only once, so Pro's higher price is paid on one call. *Against*: 3.1 Pro is a preview, so it could change or be withdrawn during judging, and its quotas are tighter.
- **B. Flash for both agents, on `global`.** Both agents use `gemini-3.8-flash`. This is the cheapest current option and uses no preview models. *Against*: the hypotheses may be weaker on hard incidents.
- **C. `gemini-3.5-flash` in `australia-southeast1` for both agents.** Nothing, including inference, leaves Australia. *Against*: it costs the most of the Flash options and uses a weaker model than 3.8 Flash.
- **D. Stay on Gemini 2.5.** It is cheap. *Against*: it may retire during judging.

### Estimated cost per incident
This assumes about 20K tokens of evidence per incident. The hypothesis writes about 3K output tokens, including thinking. The critic reads about 25K tokens and writes about 1K.

| Option | Hypothesis | Critic | Per incident | 1,000 incidents |
|---|---|---|---|---|
| A (3.1 Pro + 3.8 Flash) | ~$0.076 | ~$0.023 | **~$0.10** | ~$100 |
| B (3.8 Flash × 2) | ~$0.026 | ~$0.023 | **~$0.05** | ~$50 |
| C (3.5 Flash, Sydney × 2) | ~$0.063 | ~$0.051 | **~$0.11** | ~$115 |

The demo and judging handle tens of incidents, so every option costs a few dollars in total. Dedup also keeps the model off repeated alerts, because the model runs once per incident, not once per alert notification.

## Decision
> **A.** The **hypothesis** uses **`gemini-3.1-pro-preview`**, the best Pro model. The **critic** uses **`gemini-3.8-flash`**, the best Flash model and cheaper than 3.5 Flash. Both run on `global`, as ADR-0001 intends.
>
> - **Fallback is built in** (`asre/agent/model.py`). If 3.1 Pro Preview is missing, out of quota, overloaded, slow or returns an empty answer, the same request goes once to `gemini-3.8-flash`, and the Brief records which model answered and why. A Pro model that returned 404 or 429 is skipped for 10 minutes, so later incidents do not wait on it first. Judging never stalls on a preview model.
> - **Cost controls.** Pro runs only once per incident and never on updates or resolutions. Thinking budgets are capped: medium for the hypothesis and low for the critic. Output length is capped by the response schema. Token counts are logged on every Brief.
> - **Model IDs and location are configuration, not code.** They are set by the `models` variable in `infra/platform` (passed to ANALYSE as `ASRE_MODEL_*`). Switching between A, B and C takes a `terraform apply`, not a release. For B: `models = { hypothesis = "gemini-3.8-flash" }`. For C: `models = { location = "australia-southeast1", hypothesis = "gemini-3.5-flash", hypothesis_fallback = "", critic = "gemini-3.5-flash" }`.
> - **Live check:** `python scripts/check_models.py` calls both configured models once and prints the dated `model_version`. `--break-primary` proves the fallback answers.
> - **Live check passed (12 Oct 2026, platform project):** 3.1 Pro Preview answered in 5.9 s and 3.8 Flash in 1.6 s. With the Pro model replaced by a missing one, Flash answered in 2.7 s and the result recorded the 404 as the fallback reason. Both models report an undated `model_version` (`gemini-3.1-pro-preview`, `gemini-3.8-flash`), so there is no dated version to pin; the Brief records whatever the API reports.
> - **Before submission**, re-run `scripts/check_models.py` and check whether 3.1 Pro has gone GA. If it has, move to the GA ID.
> - **Quality check.** Run the eval set (T4.10) on A and B. If B's quality is close, choose B: it halves the cost and removes the preview risk.
>
> *Decided by: the engineering lead, 10 Oct 2026.*

## Consequences
- The hypothesis agent has **no tools** and a response schema (`asre.agent.contract.response_schema()`). Evidence is gathered by deterministic code running in parallel (`asre/evidence`), and the critic checks the result. All of it runs inside the existing ANALYSE service (`asre/analyse/pipeline.py`).
- **No ADK (12 Oct 2026).** ADK 2.9–2.11 pin `opentelemetry-api <= 1.42.1`, while `google-api-core` 2.39 needs `>= 1.44`. Adding ADK meant downgrading `google-api-core` to 2.34 and adding about 40 packages (around 150 MB) to ANALYSE during judging, against ADR-0010. The pipeline is plain Python with the same stages (evidence, hypothesis, critic), so ADK can wrap it later without changing them.
- Every Brief records the model ID and version, model location, prompt version, evidence-pack hash, token counts and whether the fallback was used.
- Only sanitised evidence reaches the model, through `asre.agent.sanitise`. It masks secrets, card numbers and email addresses, then runs a verification pass. This matters because under A the call may be processed outside Australia, which ADR-0001 accepts.
- The README and the customer-access doc say plainly that inference runs on Google's global endpoint and that stored data stays in Sydney. Option C is available for a customer who requires in-country inference.
- 3.8 Flash's introductory price ends on 1 Jan 2027. After that the cost per incident under A rises by about $0.02, and it is still below C.

## How the availability table was produced
This needs `roles/aiplatform.user` in the platform project. Run it in bash, because zsh treats `HOST` as a special variable.

```bash
source ../.asre/asre.env
bash <<'SCRIPT'
TOKEN="$(gcloud auth print-access-token)"
for M in gemini-3.1-pro-preview gemini-3.1-pro gemini-3.8-flash gemini-3.5-flash gemini-3-flash gemini-3.5-flash-lite gemini-3.1-flash-lite gemini-2.5-pro gemini-2.5-flash; do
  for L in australia-southeast1 global; do
    if [ "$L" = global ]; then API="aiplatform.googleapis.com"; else API="$L-aiplatform.googleapis.com"; fi
    CODE=$(curl -s -m 60 -o /dev/null -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      "https://$API/v1/projects/$PLATFORM/locations/$L/publishers/google/models/$M:generateContent" \
      -d '{"contents":[{"role":"user","parts":[{"text":"Reply with OK"}]}]}')
    printf '%-24s %-21s %s\n' "$M" "$L" "$CODE"
  done
done
SCRIPT
```

## Sources (checked 9 Oct 2026)
- Google Cloud, Vertex AI generative AI pricing: https://cloud.google.com/vertex-ai/generative-ai/pricing
- Google Cloud, Model versions and lifecycle: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions
- Google Cloud, Deployments and endpoints (global endpoint and data residency): https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations
- GCP Study Hub, Gemini 2.5 retirement and recommended replacements: https://gcpstudyhub.com/blog/google-is-retiring-gemini-2-5-on-agent-platform-what-you-need-to-know-and-do-before-october-2026
