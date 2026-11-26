# Entry points for humans and agents. Each target is a thin wrapper around a script; see AGENTS.md.
PY ?= .venv/bin/python
NAME ?= default
TRIALS ?= 5

.PHONY: help install test check docs-check arch-check golden-check golden-scan dev-up dev-down dev-purge dev-status dev-env logs metrics \
        runtime-check evals gate

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

install:         ## create .venv and install the package with dev extras
	python3.12 -m venv .venv && $(PY) -m pip install -e '.[dev]'

test:            ## unit + acceptance tests (needs TEST_DATABASE_URL)
	$(PY) -m pytest -q

check: docs-check arch-check golden-check  ## all static checks (no database needed)

docs-check:      ## links, AGENTS.md map, front matter, freshness
	$(PY) scripts/doc_check.py

arch-check:      ## layer direction and third-party allowlist
	$(PY) scripts/check_architecture.py

golden-check:    ## fail on drift from the golden principles that is not in the baseline
	$(PY) scripts/golden_scan.py --fail-on-new

golden-scan:     ## full drift report (new / known / fixed); writes test-results/golden-report.md
	$(PY) scripts/golden_scan.py --markdown test-results/golden-report.md

dev-up:          ## start an isolated instance: own Postgres DBs + worker + API, JSON logs in var/dev/$(NAME)/logs
	$(PY) scripts/dev_instance.py up --name $(NAME)

dev-down:        ## stop the instance (keeps data)
	$(PY) scripts/dev_instance.py down --name $(NAME)

dev-purge:       ## stop the instance and delete its data
	$(PY) scripts/dev_instance.py down --name $(NAME) --purge

dev-status:      ## processes, DB reachability and a metrics snapshot
	$(PY) scripts/dev_instance.py status --name $(NAME)

dev-env:         ## print exports; use: eval "$$(make -s dev-env)"
	@$(PY) scripts/dev_instance.py env --name $(NAME)

logs:            ## last 50 structured log records (pass ARGS="--event failed" etc.)
	$(PY) scripts/logq.py --name $(NAME) --last 50 $(ARGS)

metrics:         ## queue metrics snapshot from the instance database
	eval "$$($(PY) scripts/dev_instance.py env --name $(NAME))" && $(PY) -m personal_agent metrics $(ARGS)

runtime-check:   ## budgets (latency, DLQ, logs) + the 7 system checks against the instance
	$(PY) scripts/runtime_check.py --name $(NAME) $(ARGS)

evals:           ## eval suite (needs EVAL_DATABASE_URL)
	$(PY) -m evals.run_evals --trials $(TRIALS) --k 1 3 5 --out test-results/evals-results.json

gate:            ## regression gate; on failure write a follow-up exec plan into docs/exec-plans/active/
	@$(PY) -m evals.gate --baseline evals/baseline.json --current test-results/evals-results.json --k 3 \
	    --report-json test-results/gate-report.json || { \
	  $(PY) -m evals.followup --results test-results/evals-results.json --baseline evals/baseline.json \
	      --gate-report test-results/gate-report.json --out-dir test-results/followup \
	      --exec-plan-dir docs/exec-plans/active; exit 1; }
