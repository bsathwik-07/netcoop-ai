# Every target is a thin wrapper over the `netcoop` CLI or a script, so Windows users
# without `make` can run the same commands directly (see README "Setup").
PY ?= python

.PHONY: setup test lint schemas data run-all chaos eval dashboard mcp gateway demo clean

setup:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check netcoop tests scripts dashboard examples
	$(PY) -m mypy netcoop

schemas:
	netcoop schemas

data:
	$(PY) scripts/organizer_generator.py --write
	$(PY) scripts/generate_scenarios.py --all
	$(PY) scripts/make_malformed.py

run-all:
	netcoop run-all

chaos:
	netcoop chaos --scenario hardware_failure

eval:
	netcoop eval --seeds 50

dashboard:
	netcoop dashboard

mcp:
	netcoop mcp

gateway:
	netcoop gateway

demo:
	netcoop demo

clean:
	rm -rf reports/INC-* state/ .pytest_cache .ruff_cache .mypy_cache
