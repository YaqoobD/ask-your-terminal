.PHONY: seed demo evals test

seed:
	.venv/bin/python data/generate.py

demo:
	.venv/bin/uvicorn api.main:app --reload

evals:
	PYTHONPATH=. .venv/bin/python evals/run_evals.py

test:
	.venv/bin/python -m pytest tests -q
