.PHONY: install lint test test-integration tf-check checkov image

FIRESTORE_EMULATOR_HOST ?= 127.0.0.1:8081
LOCAL_IMAGE ?= agentic-sre:dev
TF ?= terraform

install:
	python -m venv .venv && .venv/bin/pip install -r requirements.lock -e ".[dev]"

lint:
	ruff check src tests scripts && ruff format --check src tests scripts

test:
	pytest --cov --cov-report=term-missing --cov-fail-under=80

# Needs: gcloud components install cloud-firestore-emulator
test-integration:
	@(gcloud emulators firestore start --host-port=$(FIRESTORE_EMULATOR_HOST) >/tmp/firestore-emulator.log 2>&1 &) ; \
	for i in $$(seq 1 30); do curl -sf http://$(FIRESTORE_EMULATOR_HOST) >/dev/null && break; sleep 1; done ; \
	FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) pytest tests/integration -v

tf-check:
	for d in infra/bootstrap infra/platform infra/example_customer; do \
	  (cd $$d && $(TF) fmt -check && $(TF) init -backend=false -input=false >/dev/null && $(TF) validate) || exit 1; \
	done

checkov:
	checkov -d infra --framework terraform --compact --quiet

image:
	docker build -t $(LOCAL_IMAGE) .

