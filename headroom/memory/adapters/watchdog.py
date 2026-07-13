"""Unix-socket embedding sidecar for the local sub2api Headroom image.

This is a downstream compatibility module for headroom-ai 0.31.0. The proxy CLI
already knows how to start ``EmbeddingServerWatchdog`` when
``--embedding-server`` is enabled, but the published wheel does not include the
module. The sidecar keeps one ONNX embedder process per proxy and exposes a tiny
newline-delimited JSON protocol over a Unix socket.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import numpy as np


class SocketEmbedderClient:
    """Embedder protocol implementation backed by the sidecar Unix socket."""

    DEFAULT_DIMENSION = 384
    DEFAULT_MAX_TOKENS = 256

    def __init__(self, socket_path: str, model_name: str | None = None) -> None:
        self.socket_path = socket_path
        self._model_name = model_name or "headroom-embedding-sidecar"

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()
        if not line:
            raise RuntimeError("embedding sidecar closed connection without a response")
        response = json.loads(line.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "embedding sidecar request failed")
        return response

    async def embed(self, text: str) -> np.ndarray:
        response = await self._request({"op": "embed", "text": text})
        return np.asarray(response["embedding"], dtype=np.float32)

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        response = await self._request({"op": "embed_batch", "texts": texts})
        return [np.asarray(item, dtype=np.float32) for item in response["embeddings"]]

    @property
    def dimension(self) -> int:
        return self.DEFAULT_DIMENSION

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def max_tokens(self) -> int:
        return self.DEFAULT_MAX_TOKENS

    async def close(self) -> None:
        return None


class EmbeddingServerWatchdog:
    """Start/stop a dedicated embedding server process."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._proc: subprocess.Popen[bytes] | None = None

    async def start(self) -> None:
        path = Path(self.socket_path)
        try:
            if path.exists():
                path.unlink()
        except FileNotFoundError:
            pass
        env = os.environ.copy()
        # Avoid recursive client selection inside the sidecar process.
        env.pop("HEADROOM_EMBEDDING_SERVER_SOCKET", None)
        env["HEADROOM_EMBEDDING_SERVER_CHILD"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "headroom.memory.adapters.watchdog", "serve", self.socket_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    async def wait_until_healthy(self, timeout: float = 30.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                try:
                    writer.write(b'{"op":"health"}\n')
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                finally:
                    writer.close()
                    await writer.wait_closed()
                if line and json.loads(line.decode("utf-8")).get("ok"):
                    return True
            except Exception:
                await asyncio.sleep(0.25)
        return False

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self._proc.wait), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await asyncio.to_thread(self._proc.wait)
        try:
            Path(self.socket_path).unlink()
        except FileNotFoundError:
            pass


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, embedder: Any) -> None:
    try:
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        op = request.get("op")
        if op == "health":
            response = {"ok": True, "dimension": 384, "model": "onnx-sidecar"}
        elif op == "embed":
            embedding = await embedder.embed(str(request.get("text") or ""))
            response = {"ok": True, "embedding": embedding.astype(np.float32).tolist()}
        elif op == "embed_batch":
            texts = request.get("texts")
            if not isinstance(texts, list):
                raise ValueError("texts must be a list")
            embeddings = await embedder.embed_batch([str(item) for item in texts])
            response = {
                "ok": True,
                "embeddings": [item.astype(np.float32).tolist() for item in embeddings],
            }
        else:
            raise ValueError(f"unknown op: {op}")
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}
    writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _serve(socket_path: str) -> None:
    from headroom.memory.adapters.embedders import OnnxLocalEmbedder

    path = Path(socket_path)
    try:
        if path.exists():
            path.unlink()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    embedder = OnnxLocalEmbedder()
    server = await asyncio.start_unix_server(
        lambda reader, writer: _handle_client(reader, writer, embedder),
        path=socket_path,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "serve":
        raise SystemExit("usage: python -m headroom.memory.adapters.watchdog serve <socket_path>")
    asyncio.run(_serve(sys.argv[2]))


if __name__ == "__main__":
    main()
