#!/usr/bin/env bash
# Crea el entorno virtual e instala las dependencias (macOS / Linux).
#   ./install.sh          -> solo lo necesario para ejecutar
#   ./install.sh --dev    -> añade pytest para ejecutar las pruebas
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "No se encuentra Python 3. En macOS: 'brew install python' o https://www.python.org/downloads/" >&2
  exit 1
fi
"$PY" - <<'PYEOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Se necesita Python 3.10 o superior (detectado {sys.version.split()[0]}).")
PYEOF
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
if [ "${1:-}" = "--dev" ]; then
  .venv/bin/python -m pip install -r requirements-dev.txt
else
  .venv/bin/python -m pip install -r requirements.txt
fi
echo "Instalación completada. Inicie la aplicación con ./run.sh"
