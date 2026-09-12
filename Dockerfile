FROM python:3.12-slim

# Install Node.js 20
RUN apt-get update \
    && apt-get install -y curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install WebCMD
RUN npm install -g @agentrhq/webcmd \
    && npx -y playwright install-deps chromium \
    && npx -y playwright install chromium

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]