FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCILAB_BINARY=scilab-adv-cli

WORKDIR /app

# scilab-cli: core CLI stack; scilab-full-bin: graphics/JOGL for headless plot export.
# xvfb + X11/GL libs: virtual display so plot/plot2d do not fail without a real screen.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        scilab-cli \
        scilab-full-bin \
        xvfb \
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
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
