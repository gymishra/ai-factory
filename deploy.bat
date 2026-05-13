@echo off
REM Deploy AI Factory to Fargate
REM Run from: C:\bin\sap-ai-factory

set AWS_ACCOUNT=953841955037
set AWS_REGION=us-east-1
set ECR_REPO=%AWS_ACCOUNT%.dkr.ecr.%AWS_REGION%.amazonaws.com/sap-ai-factory-mcp

echo === Logging into ECR ===
aws ecr get-login-password --region %AWS_REGION% | docker login --username AWS --password-stdin %AWS_ACCOUNT%.dkr.ecr.%AWS_REGION%.amazonaws.com

echo === Building Docker image ===
docker build -t sap-ai-factory-mcp .

echo === Tagging and pushing to ECR ===
docker tag sap-ai-factory-mcp:latest %ECR_REPO%:latest
docker push %ECR_REPO%:latest

echo === Forcing new Fargate deployment ===
aws ecs update-service --cluster sap-ai-factory --service sap-mcp-server --force-new-deployment --region %AWS_REGION%

echo === Done! New container will roll out in ~2 minutes ===
