# Dev-box targets. On bigpc use the project venv (.venv); on pi4 the system
# python has all deps installed.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: test lint deploy

test:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m compileall -q app run.py
	node --check static/index.js
	node --check static/dashboard.js

deploy:
	bash scripts/deploy-v2.sh
