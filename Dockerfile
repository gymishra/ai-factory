FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MCP_PORT=8000
ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1

EXPOSE 8000

CMD ["python", "agents/start_all.py"]
