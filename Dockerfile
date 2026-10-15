FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -m app && mkdir -p /app/logs && chown app /app/logs
USER app
ENV PORT=8000 MF_AUDIT_LOG=/app/logs/audit.jsonl
EXPOSE 8000
CMD ["mf-mcp", "--transport", "http"]
