"""Disposable loopback nginx in front of the same ASGI app, using no static asset alias."""

import socket
import subprocess
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn


@contextmanager
def proxy_client(app, root):
    with socket.socket() as backend:
        backend.bind(("127.0.0.1", 0))
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [backend]}, daemon=True)
        thread.start()
        # All URIs flow to the reader. Private byte storage is outside the proxy root.
        config = root / "nginx.conf"
        config.write_text(f"""daemon off;
pid {root}/pid;
error_log stderr;
events {{}}
http {{
  access_log off;
  client_body_temp_path {root}/client;
  proxy_temp_path {root}/proxy;
  fastcgi_temp_path {root}/fastcgi;
  uwsgi_temp_path {root}/uwsgi;
  scgi_temp_path {root}/scgi;
  server {{
    listen 127.0.0.1:{port};
    location / {{ proxy_pass http://127.0.0.1:{backend.getsockname()[1]}; }}
  }}
}}
""")
        try:
            with (
                subprocess.Popen(["nginx", "-p", str(root), "-c", str(config)]) as proxy,
                httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
            ):
                try:
                    for _ in range(100):
                        if proxy.poll() is not None:
                            raise RuntimeError("nginx failed to start")
                        try:
                            if client.get("/missing").status_code == 404:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("nginx/reader did not become ready")
                    yield client
                finally:
                    proxy.terminate()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
