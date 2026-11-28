.PHONY: lint fmt test guide demo-up demo-down demo-logs demo cover slides

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

test:
	pytest -m "not docker and not llm and not attacks"

guide:
	python3 scripts/check_guide.py

# One-command startup (docs/clients.md, "docker compose"). Needs Docker, the MiniLocker submodule and a .env with the two tokens.
demo-up:
	git submodule update --init third_party/minilocker
	@test -f .env || { echo "no .env: copy .env.example to .env and set TOOLFORGE_SERVER_TOKEN and TOOLFORGE_DASHBOARD_TOKEN"; exit 1; }
	docker compose up -d --build
	@echo "MCP server: http://127.0.0.1:8765/mcp   dashboard: http://127.0.0.1:8787/   (bearer tokens from .env)"

demo-down:
	docker compose down

demo-logs:
	docker compose logs -f server dashboard

# The scripted demo: a scripted agent, a real server, real gates and a real sandbox (docs/DEMO_SCRIPT.md). Needs Docker and `toolforge images build`.
demo:
	python demo/run_demo.py --replay demo/transcripts/

# The cover image and the slide outline (docs/assets/cover.svg, docs/slides.md).
cover:
	python scripts/render_cover.py

slides:
	python scripts/build_slides.py
