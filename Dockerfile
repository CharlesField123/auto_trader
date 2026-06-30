# Railway Auto-Trader — lightweight Python image, no browser required.
FROM python:3.12-slim

# Avoid .pyc files and unbuffered stdout so Railway logs stream live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY . .

# Default to the continuous 24/5 crypto scanner (1-minute interval). Railway's
# startCommand can override this; run `python main.py` for the daily stock bot.
CMD ["python", "main.py", "--crypto"]
