# Convenience wrappers. `make demo` is the single command to hand the client.
PY := .venv/bin/python

.PHONY: demo install test smoke run clean

demo:        ## Full validation: setup + smoke + tests + artifacts (one command)
	./demo.sh

install:     ## Create the venv and install pinned deps
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt

test:        ## Run the unit tests (needs `make install` first)
	$(PY) -m pytest -q

smoke:       ## Run the Step 2 butter task end-to-end
	$(PY) smoke_test.py

run:         ## Launch the MCP server over stdio
	$(PY) server.py

clean:       ## Remove venv, caches, and the local audit log
	rm -rf .venv .pytest_cache __pycache__ tests/__pycache__ audit.log
