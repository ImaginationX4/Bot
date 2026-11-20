$ErrorActionPreference = 'Stop'
uv run --with 'streamlit==1.67.0' streamlit run (Join-Path $PSScriptRoot 'app.py') --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
exit $LASTEXITCODE
