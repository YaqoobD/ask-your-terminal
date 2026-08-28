.PHONY: seed demo evals test

seed:
	.venv/bin/python data/generate.py

demo:
	.venv/bin/uvicorn api.main:app --reload

evals:
	.venv/bin/python -m pytest evals -q

test:
	.venv/bin/python -m pytest tests -q
