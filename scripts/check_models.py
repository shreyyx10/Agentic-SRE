"""Call the configured Gemini models once and print what answered (ADR-0002).

Run it from the repository root with your own credentials (``roles/aiplatform.user``
on the platform project):

    source ../.asre/asre.env
    GOOGLE_CLOUD_PROJECT="$PLATFORM" python scripts/check_models.py
    GOOGLE_CLOUD_PROJECT="$PLATFORM" python scripts/check_models.py --break-primary

It uses the same ``ASRE_MODEL_*`` settings and fallback logic as ANALYSE.
``--break-primary`` swaps the hypothesis model for one that does not exist, to
prove live that the fallback answers. Each line shows the dated ``model_version``
to record in ADR-0002 before submission.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from asre.agent.model import ModelError, build_models
from asre.core.config import ModelSettings

SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--break-primary", action="store_true", help="use a missing hypothesis model")
    args = parser.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("set GOOGLE_CLOUD_PROJECT (the platform project)", file=sys.stderr)
        return 2
    settings = ModelSettings.from_env()
    if args.break_primary:
        settings = replace(settings, hypothesis="gemini-does-not-exist")
    hypothesis, critic = build_models(settings, project)

    failed = False
    for role, model in (("hypothesis", hypothesis), ("critic", critic)):
        try:
            result = model.generate("Reply with JSON only.", 'Return {"status": "ok"}.', SCHEMA)
            print(json.dumps({"role": role, "answer": json.loads(result.text), **result.as_record()}))
        except (ModelError, ValueError) as exc:
            failed = True
            print(json.dumps({"role": role, "error": str(exc)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
