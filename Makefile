.PHONY: install test probe pilot preview clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q
	node --check web/viewer.js

probe:
	gbg probe

pilot:
	gbg enumerate pilot && gbg stats

preview:
	python scripts/make_sample_voxels.py
	@echo "open web/index.html via a local server, e.g.: python -m http.server -d web 8000"

clean:
	rm -rf data/derived data/gbg.duckdb .pytest_cache
