.PHONY: seed demo evals test

seed:
	.venv/bin/python data/generate.py

demo:
	test -f data/terminal.duckdb || .venv/bin/python data/generate.py
	( sleep 1 && open http://127.0.0.1:8000 ) &
	.venv/bin/uvicorn api.main:app --reload

evals:
	PYTHONPATH=. .venv/bin/python evals/run_evals.py

test:
	.venv/bin/python -m pytest tests -q
