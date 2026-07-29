#!/bin/bash
echo "Installing headless browser system dependencies..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
    libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 \
    libxtst6 libegl1 libgl1-mesa-dri libgbm1 xvfb \
    fonts-liberation ca-certificates
