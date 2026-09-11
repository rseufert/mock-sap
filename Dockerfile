FROM python:3.12-slim
WORKDIR /app
COPY mocksap ./mocksap
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir .
EXPOSE 8000
ENTRYPOINT ["mock-sap", "--host", "0.0.0.0", "--port", "8000"]
