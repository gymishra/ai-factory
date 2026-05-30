FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

# Configure UV for container environment
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1

# Cache-bust: bump this to force a fresh dependency resolution
ENV DEP_VERSION=8

COPY requirements.txt requirements.txt
# Install from requirements file (no cache, strict resolution)
RUN uv pip install --no-cache --reinstall -r requirements.txt

RUN uv pip install aws-opentelemetry-distro>=0.10.1

# Verify the installed mcp version at build time (fail loudly if wrong)
RUN python -c "import importlib.metadata as m; v=m.version('mcp'); print('MCP VERSION:', v); assert v.startswith('1.1'), f'WRONG MCP {v}'"

# Set AWS region environment variable
ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1
ENV MCP_PORT=8000

# Signal that this is running in Docker for host binding logic
ENV DOCKER_CONTAINER=1

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080
EXPOSE 8000

# Copy entire project (respecting .dockerignore)
COPY . .

# Use the full module path
CMD ["opentelemetry-instrument", "python", "agents/start_all.py"]
