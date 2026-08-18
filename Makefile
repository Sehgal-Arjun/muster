VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

# talon must be installed with --no-deps. Its metadata hard-requires cchardet,
# whose C extension cannot build on Python 3.11+; faust-cchardet (pulled in by
# the `parse` extra) supplies the same `import cchardet`. See pyproject.toml.
# If you ever rebuild the environment by hand, this is the step to remember.
TALON := talon==1.4.4

.PHONY: install
install: $(VENV)
	$(PIP) install -e ".[dev,gmail,parse]"
	$(PIP) install --no-deps $(TALON)
	@$(PY) -c "import talon, cchardet; assert not talon.ML_ENABLED; print('talon ok (ML off)')"

.PHONY: install-embed
install-embed: install
	$(PIP) install -e ".[embed]"

$(VENV):
	python3.12 -m venv $(VENV)

.PHONY: test
test:
	$(PY) -m pytest -q

.PHONY: fixtures
fixtures:
	$(PY) tests/fixtures/make_fixtures.py

.PHONY: clean
clean:
	rm -rf .pytest_cache **/__pycache__
