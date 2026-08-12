FROM python:3.11-slim

# Install system dependencies for headless browser operation
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libasound2 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    wget \
    unzip \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy solver files
COPY Boterdrop-Solver/ /app/

# Remove the venv if copied
RUN rm -rf /app/venv /app/debug_logs /app/solver.log

# Install Python dependencies
RUN pip install --no-cache-dir \
    fastapi==0.95.2 \
    uvicorn \
    "camoufox[fetch]" \
    loguru \
    psutil \
    playwright

# Fetch Camoufox browser
RUN python3 -m camoufox fetch

# Install Playwright dependencies
RUN python3 -m playwright install-deps chromium || true
RUN playwright install chromium

# Install Xvfb dependencies
RUN apt-get install -y xvfb

# Remove the interactive config prompt
RUN sed -i 's/config = _interactive_config(config)/pass/' api_server.py

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/result?id=health || exit 1

# Start the solver with xvfb
CMD ["sh", "-c", "xvfb-run -a --server-args='-screen 0 1920x1080x24 -ac' python3 api_server.py"]
