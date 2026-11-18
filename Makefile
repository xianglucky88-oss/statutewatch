PY=python3
run:
	PYTHONPATH=src $(PY) -m statutewatch.cli
run-regulations:
	PYTHONPATH=src $(PY) -m statutewatch.cli --regulations
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
