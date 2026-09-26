#!/usr/bin/env python3
"""Minimal stand-in for Mac 9router: dashboard HTML + /v1/models."""
from http.server import BaseHTTPRequestHandler, HTTPServer


LOGIN_HTML = b"""<!DOCTYPE html><html lang="en"><head><title>9Router Login</title></head>
<body><h1>9Router</h1><form action="/auth/login" method="post"><input name="password"/></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(307)
            self.send_header("Location", "/dashboard")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/dashboard", "/login"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(LOGIN_HTML)))
            self.send_header("Set-Cookie", "session=edge-proof; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(LOGIN_HTML)
            return
        if path == "/_next/static/proof.js":
            body = b"console.log('ok')"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v1/models":
            body = b'{"object":"list","data":[{"id":"subs-coding"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9000), Handler).serve_forever()
