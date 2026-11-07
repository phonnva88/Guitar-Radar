PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
GO ?= $(if $(wildcard /workspace/.tools/go/bin/go),/workspace/.tools/go/bin/go,go)
export GOPATH ?= /workspace/.cache/go
export GOCACHE ?= /workspace/.cache/go-build
export PIP_CACHE_DIR ?= /workspace/.cache/pip
GROUP ?=
PAGES ?= 2
PAGE_SIZE ?= 20
TIMEOUT ?= 120
ARGS = $(if $(GROUP),--group "$(GROUP)",) --pages $(PAGES) --page-size $(PAGE_SIZE) --timeout $(TIMEOUT)

.PHONY: setup build test phase0 collect-facebook-go collect-fbgql normalize compare demo
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
