PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
TOOLS_DIR ?= $(CURDIR)/.tools
CACHE_DIR ?= $(CURDIR)/.cache
GO ?= $(if $(wildcard $(TOOLS_DIR)/go/bin/go),$(TOOLS_DIR)/go/bin/go,go)
export GOPATH ?= $(CACHE_DIR)/go
export GOCACHE ?= $(CACHE_DIR)/go-build
export PIP_CACHE_DIR ?= $(CACHE_DIR)/pip
GROUP ?=
PAGES ?= 2
PAGE_SIZE ?= 20
TIMEOUT ?= 120
GROUP_ID ?= $(GROUP)
RUNS ?= 10
MAX_PAGES ?= 50
INTERVAL ?= 30
WINDOW_TIMEOUT ?= 300
FROM ?= 2026-11-06T00:00:00+07:00
TO ?= now
WINDOW_ARGS = $(if $(GROUP_ID),--group "$(GROUP_ID)",) --from "$(FROM)" --to "$(TO)" --max-pages $(MAX_PAGES) --timeout $(WINDOW_TIMEOUT)
ARGS = $(if $(GROUP),--group "$(GROUP)",) --pages $(PAGES) --page-size $(PAGE_SIZE) --timeout $(TIMEOUT)

.PHONY: setup build test phase0 collect-facebook-go collect-fbgql normalize compare demo benchmark-window benchmark-repeat validation-demo validation-analyze
setup:
	bash scripts/setup.sh
build:
	mkdir -p .bin
	cd collectors/facebook-go && $(GO) build -mod=readonly -o ../../.bin/facebook-go .
test: build
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m unittest discover -s collectors/fbgql -p 'test_*.py' -v
	cd collectors/facebook-go && $(GO) test -race -mod=readonly ./...
phase0: build
	$(PYTHON) scripts/phase0.py $(ARGS)
collect-facebook-go: build
	$(PYTHON) scripts/phase0.py --collector facebook-go $(ARGS)
collect-fbgql:
	$(PYTHON) scripts/phase0.py --collector fbgql $(ARGS)
normalize:
	$(PYTHON) -m normalizer --raw-dir data/raw --output-dir data/normalized
compare:
	$(PYTHON) -m compare --input-dir data/normalized --output-dir reports
demo:
	$(PYTHON) scripts/phase0.py --demo
benchmark-window: build
	$(PYTHON) -m validation window $(WINDOW_ARGS)
benchmark-repeat: build
	$(PYTHON) -m validation repeat $(WINDOW_ARGS) --runs $(RUNS) --interval $(INTERVAL)
validation-demo:
	$(PYTHON) -m validation demo
validation-analyze:
	$(PYTHON) -m validation analyze

.PHONY: phase1-setup phase1 phase1-backup
phase1-setup:
	bash scripts/phase1.sh setup
phase1:
	bash scripts/phase1.sh start
phase1-backup:
	bash scripts/phase1.sh backup
