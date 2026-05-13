# SAP AI Factory

Multi-agent MCP server for SAP S/4HANA, Cloud ALM, SuccessFactors, and ABAP development.

## Architecture

```
Parent MCP Server (port 8000)
├── ADT Agent       (port 8101) — ABAP source, syntax check, ATC, transports
├── OData Agent     (port 8102) — S/4HANA OData, NL→query, catalog caching
├── Cloud ALM Agent (port 8103) — Projects, tasks, features, monitoring
├── SF Agent        (port 8104) — Employee Central, recruiting, learning
└── Generator Agent (port 8105) — Generates & deploys new MCP servers
```

## Quick Start

```bash
pip install -r requirements.txt
python agents/start_all.py
```

MCP endpoint: `http://localhost:8100/mcp`

## Docker / Fargate

```bash
docker build -t sap-ai-factory-mcp .
docker run -p 8000:8000 \
  -e SAP_BASE_URL=https://your-sap-host \
  -e AWS_REGION=us-east-1 \
  sap-ai-factory-mcp
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SAP_BASE_URL` | SAP S/4HANA base URL |
| `OKTA_DOMAIN` | Okta tenant for auth |
| `OKTA_CLIENT_ID` | Okta client ID |
| `MCP_PORT` | Parent MCP port (default: 8100, container: 8000) |
| `AI_FACTORY_DATA_DIR` | Persistent storage path (default: /data) |

## Deploy to AWS Fargate

```bash
./deploy.sh
```
