# Convenience wrappers. `make demo` is the single command to hand the client.
PY := .venv/bin/python

.PHONY: demo install test smoke run cli cli-docker clean

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

cli:         ## Interactive command-line client (in-memory server)
	$(PY) cli.py

cli-docker:  ## Interactive command-line client against the Docker container
	$(PY) cli.py --docker

clean:       ## Remove venv, caches, and the local audit log
	rm -rf .venv .pytest_cache __pycache__ tests/__pycache__ audit.log
