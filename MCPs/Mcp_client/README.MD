推荐执行测试
uv python install 3.13
uv sync --frozen

docker build `
  --provenance=false `
  --sbom=false `
  --file Sandbox/Dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.3 `
  .

uv run python -m MCPs.Mcp_client.debug_client
