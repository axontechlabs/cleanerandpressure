"""Arranque de la aplicación web local.

Uso:  python -m app.main [--host 127.0.0.1] [--port 8000] [--no-browser]

Si el puerto está ocupado (casi siempre, una copia anterior del programa que sigue abierta en otra
ventana de Terminal), se avisa y se usa el siguiente puerto libre: así el navegador nunca abre la
versión antigua por error.
"""
from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
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


def ask_api_key() -> None:
    """Al arrancar desde la Terminal: comprueba la clave de Claude y, si no hay o no funciona, la pide."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import ai_client
    print("  Conexión con Claude (IA)…")
    st = ai_client.test_connection() if ai_client.get_api_key() else ai_client.status()
    while not st["connected"]:
        if st.get("error") and st.get("configured"):
            print(f"  La clave guardada no conecta: {st['error']}")
        print("  Pegue la clave de la API de Claude (empieza por sk-ant-) y pulse Intro."
              "\n  Al pegarla no se ve nada en pantalla: es normal. Intro vacío = seguir sin IA.")
        try:
            key = getpass.getpass("  Clave: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not key:
            print("  Sin IA por ahora: la página también le pedirá la clave.")
            return
        try:
            ai_client.set_api_key(key, model=ai_client.get_model(), persist=False)
        except ai_client.AIError as e:
            print(f"  {e}")
            continue
        st = ai_client.test_connection()
        if st["connected"]:          # solo se guarda una clave que funciona
            st = {**ai_client.set_api_key(key, model=ai_client.get_model(), persist=True), "connected": True}
        else:
            print(f"  No conecta: {st['error']}")
            st["configured"] = False
    print(f"  IA conectada · {st['model']} (clave {st['key_hint']}, guardada en este ordenador)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Procesador de mallas plantares (interfaz web local)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para Docker/Codespaces")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    ap.add_argument("--no-ask", action="store_true", help="no pedir la clave de la IA en la Terminal")
    args = ap.parse_args()
    if not args.no_ask and sys.stdin.isatty():
        try:
            ask_api_key()
        except Exception as e:  # noqa: BLE001 - la página la pedirá igualmente
            print(f"  No se ha podido comprobar la IA desde la Terminal: {e}")
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
