"""Cliente mínimo de la API de Claude (Anthropic) para la revisión por IA.

Solo usa la biblioteca estándar (``urllib``): no añade dependencias. La clave se busca, por
este orden, en:

1. la variable de entorno ``ANTHROPIC_API_KEY``;
2. el archivo ``.env`` de la raíz del proyecto (línea ``ANTHROPIC_API_KEY=...``), que está en
   ``.gitignore`` y nunca se sube a GitHub;
3. la que se introduzca en la interfaz (solo en memoria, salvo que se pida guardarla en ``.env``).

El modelo por defecto es ``claude-sonnet-5-5`` (buena relación calidad/precio y con visión); se
puede cambiar con ``ANTHROPIC_MODEL`` o desde la interfaz.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_runtime_key: str | None = None
_runtime_model: str | None = None


class AIError(RuntimeError):
    """Error de la revisión por IA que debe mostrarse al usuario."""


def _read_env_file() -> dict:
    out = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def get_api_key() -> str | None:
    return (os.environ.get("ANTHROPIC_API_KEY") or _runtime_key or _read_env_file().get("ANTHROPIC_API_KEY")
            or None)


def get_model() -> str:
    return (_runtime_model or os.environ.get("ANTHROPIC_MODEL") or _read_env_file().get("ANTHROPIC_MODEL")
            or DEFAULT_MODEL)


def set_api_key(key: str | None, model: str | None = None, persist: bool = False) -> dict:
    """Fija la clave (y el modelo) para esta sesión; con ``persist`` la guarda en ``.env``."""
    global _runtime_key, _runtime_model
    key = (key or "").strip() or None
    if key is not None and not key.startswith("sk-"):
        raise AIError("La clave no parece válida: las claves de la API de Claude empiezan por «sk-».")
    _runtime_key = key
    if model:
        _runtime_model = model.strip()
    if persist and key:
        env = _read_env_file()
        env["ANTHROPIC_API_KEY"] = key
        if model:
            env["ANTHROPIC_MODEL"] = model.strip()
        ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass
    return status()


def status() -> dict:
    k = get_api_key()
    src = ("variable de entorno" if os.environ.get("ANTHROPIC_API_KEY") else
           "interfaz (sesión)" if _runtime_key else ".env" if k else None)
    return {"configured": bool(k), "source": src, "model": get_model(),
            "key_hint": (k[:7] + "…" + k[-4:]) if k else None}


def parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON de la respuesta (admite bloques ```json)."""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts[1::2]:
            p = p[4:] if p.lower().startswith("json") else p
            try:
                return json.loads(p.strip())
            except json.JSONDecodeError:
                continue
    i = t.find("{")
    while i >= 0:
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(t)):
            c = t[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except json.JSONDecodeError:
                        break
        i = t.find("{", i + 1)
    raise AIError("La IA no ha devuelto un JSON válido: " + text[:300])


class ClaudeClient:
    """Llamadas a la API de mensajes con texto e imágenes PNG; respuesta en JSON.

    ``transport`` permite sustituir la llamada HTTP (pruebas sin red): recibe el cuerpo de la
    petición (dict) y devuelve el cuerpo de la respuesta (dict).
    """

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 180.0,
                 transport=None, max_retries: int = 2):
        self.api_key = api_key or get_api_key()
        self.model = model or get_model()
        self.timeout = timeout
        self.transport = transport
        self.max_retries = max_retries
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        if not self.api_key and transport is None:
            raise AIError("No hay clave de la API de Claude configurada. Introdúzcala en la interfaz o defina "
                          "ANTHROPIC_API_KEY (vea README → Revisión por IA).")

    def _post(self, body: dict) -> dict:
        if self.transport is not None:
            return self.transport(body)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(API_URL, data=data, method="POST", headers={
            "x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                try:
                    msg = json.loads(detail).get("error", {}).get("message", detail)
                except json.JSONDecodeError:
                    msg = detail
                if e.code in (429, 500, 502, 503, 529) and attempt < self.max_retries:
                    last = msg
                    time.sleep(4.0 * (attempt + 1))
                    continue
                if e.code == 401:
                    raise AIError("La API ha rechazado la clave (401). Compruebe que es correcta y está activa.") from e
                if e.code == 403:
                    raise AIError(f"Sin permiso para usar la API (403): {msg}") from e
                if "credit" in msg.lower() or "balance" in msg.lower():
                    raise AIError("La cuenta de la API no tiene créditos suficientes. Compre créditos en la "
                                  "Claude Console (Settings → Billing).") from e
                raise AIError(f"Error de la API ({e.code}): {msg}") from e
            except urllib.error.URLError as e:
                last = str(e.reason)
                if attempt < self.max_retries:
                    time.sleep(3.0)
                    continue
                raise AIError(f"No se puede conectar con la API de Claude: {e.reason}") from e
        raise AIError(f"La API no responde: {last}")

    def ask_json(self, system: str, text: str, images: list[bytes] | None = None,
                 max_tokens: int = 6000) -> dict:
        content = []
        for png in images or []:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(png).decode("ascii")}})
        content.append({"type": "text", "text": text})
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": content}]}
        resp = self._post(body)
        u = resp.get("usage") or {}
        self.usage["input_tokens"] += int(u.get("input_tokens", 0) or 0)
        self.usage["output_tokens"] += int(u.get("output_tokens", 0) or 0)
        self.usage["calls"] += 1
        out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        if not out.strip():
            raise AIError("La IA ha devuelto una respuesta vacía.")
        return parse_json(out)
