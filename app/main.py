"""Arranque de la aplicación web local.

Uso:  python -m app.main [--host 127.0.0.1] [--port 8000] [--no-browser]

Si el puerto está ocupado (casi siempre, una copia anterior del programa que sigue abierta en otra
ventana de Terminal), se avisa y se usa el siguiente puerto libre: así el navegador nunca abre la
versión antigua por error.
"""
from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

import uvicorn


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host if host not in ("0.0.0.0", "::") else "0.0.0.0", port))
            return True
        except OSError:
            return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Procesador de mallas plantares (interfaz web local)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para Docker/Codespaces")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    args = ap.parse_args()
    port = args.port
    if not _port_free(args.host, port):
        free = next((p for p in range(port + 1, port + 50) if _port_free(args.host, p)), None)
        print(f"\n  AVISO: el puerto {port} está ocupado. Seguramente sigue abierta una copia ANTERIOR del programa"
              f"\n  en otra ventana de Terminal (ciérrela con Ctrl+C), o ciérrela con:"
              f"\n      lsof -ti tcp:{port} | xargs kill"
              f"\n  No use http://127.0.0.1:{port}: ahí está la versión antigua.")
        if free is None:
            raise SystemExit("  No hay ningún puerto libre cerca; cierre la copia anterior y vuelva a intentarlo.")
        port = free
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{port}"
    print(f"\n  Procesador de mallas plantares -> {url}\n  (Ctrl+C para salir)\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
