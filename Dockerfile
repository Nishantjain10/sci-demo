FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCILAB_BINARY=scilab-adv-cli

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        scilab \
        scilab-cli \
        scilab-full-bin \
        xvfb \
        xauth \
        libgl1-mesa-glx \
        libglu1-mesa \
        libxext6 \
        libxrender1 \
        libxt6 \
        libxi6 \
        libxrandr2 \
        libxxf86vm1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt

COPY main.py index.html .

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10000/health')" || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
