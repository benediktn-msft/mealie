FROM python:3.12-slim

LABEL org.opencontainers.image.title="Mealie AI Companion"
LABEL org.opencontainers.image.description="LLM-powered sidecar for Mealie (Copilot-backed)"
LABEL org.opencontainers.image.source="https://github.com/benediktn-msft/mealie"

WORKDIR /app

RUN pip install --no-cache-dir \
        "fastapi==0.115.4" \
        "uvicorn[standard]==0.32.0" \
        "httpx==0.27.2" \
        "pydantic==2.9.2"

COPY app.py .

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8081/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
