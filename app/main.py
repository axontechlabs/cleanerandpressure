"""Arranque de la aplicación web local.

Uso:  python -m app.main [--host 127.0.0.1] [--port 8000] [--no-browser]
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="Procesador de mallas plantares (interfaz web local)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para Docker/Codespaces")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    args = ap.parse_args()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  Procesador de mallas plantares -> {url}\n  (Ctrl+C para salir)\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
