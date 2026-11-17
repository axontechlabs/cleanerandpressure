#!/usr/bin/env bash
# Inicia la aplicación web local (instala las dependencias la primera vez).
#   ./run.sh                              -> http://127.0.0.1:8000 y abre el navegador
#   ./run.sh --port 8080 --no-browser
#   ./run.sh --host 0.0.0.0 --no-browser  -> Codespaces / Docker / red local
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import numpy, scipy, starlette, uvicorn, matplotlib" 2>/dev/null; then
  bash ./install.sh
fi
exec .venv/bin/python -m app.main "$@"
