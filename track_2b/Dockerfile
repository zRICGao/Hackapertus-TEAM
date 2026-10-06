FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

CMD ["python", "-m", "apertus_lab.cli", "run-mock", "--spec", "src/examples/mock-spec.json", "--responses", "src/examples/mock-responses.json", "--store", "/tmp/apertus-run"]
