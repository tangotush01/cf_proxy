# Use an official lightweight Python image
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies required for Firefox (Camoufox) and Xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libasound2 \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libegl1 \
    libgl1-mesa-dri \
    libgbm1 \
    xvfb \
    fonts-liberation \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached unless requirements.txt changes
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-fetch the Camoufox browser binary + fingerprint db at build time
RUN python -m camoufox fetch && \
    python -c "from camoufox.pkgman import installed_verstr; print('Installed:', installed_verstr())"

COPY app.py /app/

EXPOSE 5000

# Camoufox spawns its own Xvfb internally via headless="virtual",
# so no need to wrap this in xvfb-run
CMD ["python", "app.py"]