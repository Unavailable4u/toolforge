.PHONY: lint fmt test guide

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

test:
	pytest -m "not docker and not llm and not attacks"

guide:
	python3 scripts/check_guide.py
