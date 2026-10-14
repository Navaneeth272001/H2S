FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent code
COPY . .

# Run the agent in non-strict package mode for development / testing
CMD ["python", "-m", "h2s_agent", "run", "--config", "/etc/h2s-agent/agent.json", "--allow-unsigned-package"]
