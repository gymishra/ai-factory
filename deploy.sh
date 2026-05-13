#!/bin/bash
# Deploy AI Factory to Fargate
# Run from /mnt/c/bin/sap-ai-factory

AWS_ACCOUNT=953841955037
AWS_REGION=us-east-1
ECR_REPO="$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/sap-ai-factory-mcp"

echo "=== Logging into ECR ==="
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

echo "=== Building Docker image ==="
cd /mnt/c/bin/sap-ai-factory
docker build -t sap-ai-factory-mcp .

echo "=== Pushing to ECR ==="
docker tag sap-ai-factory-mcp:latest $ECR_REPO:latest
docker push $ECR_REPO:latest

echo "=== Forcing new Fargate deployment ==="
aws ecs update-service --cluster sap-ai-factory --service sap-mcp-server --force-new-deployment --region $AWS_REGION

echo "=== Done! New container rolls out in ~2 minutes ==="
