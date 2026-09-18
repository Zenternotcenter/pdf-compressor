import sys
import os
import traceback

# Add root workspace directory to sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from app import app
except Exception as e:
    from flask import Flask, Response
    err_tb = traceback.format_exc()
    app = Flask(__name__)
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def catch_all_error(path):
        return Response(
            f"""<!DOCTYPE html>
<html>
<head><title>Startup Error Diagnostic</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 30px; background: #0f172a; color: #f8fafc; }}
.card {{ background: #1e293b; border-radius: 12px; padding: 24px; max-width: 900px; margin: 0 auto; border: 1px solid #334155; }}
h1 {{ color: #ef4444; font-size: 24px; margin-top: 0; }}
pre {{ background: #0f172a; color: #fca5a5; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 14px; line-height: 1.5; }}
</style>
</head>
<body>
<div class="card">
    <h1>⚠️ Server Startup Exception</h1>
    <p>The application encountered an error during initialization:</p>
    <pre>{err_tb}</pre>
</div>
</body>
</html>""",
            mimetype="text/html",
            status=500
        )


# WSGI Middleware to restore original request path from Vercel rewrites
class VercelPathFixMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # 1. First priority: Check __path__ passed by vercel.json rewrite
        qs = environ.get("QUERY_STRING", "")
        if "__path__=" in qs:
            import urllib.parse
            params = urllib.parse.parse_qs(qs)
            if "__path__" in params and params["__path__"]:
                path = params["__path__"][0]
                if not path.startswith("/"):
                    path = "/" + path
                environ["PATH_INFO"] = path
                clean_params = [(k, v) for k, vs in params.items() if k != "__path__" for v in vs]
                environ["QUERY_STRING"] = urllib.parse.urlencode(clean_params)
                return self.wsgi_app(environ, start_response)

        # 2. Check headers
        candidates = [
            environ.get("HTTP_X_FORWARDED_URI"),
            environ.get("REQUEST_URI"),
            environ.get("RAW_URI"),
        ]
        
        real_url = None
        for c in candidates:
            if c and not c.startswith("/api/index"):
                real_url = c
                break
                
        if real_url:
            path_only = real_url.split("?")[0]
            environ["PATH_INFO"] = path_only
        else:
            current_path = environ.get("PATH_INFO", "")
            if current_path in ("/api/index", "/api/index/", "/api/index.py", "/api", "/api/"):
                environ["PATH_INFO"] = "/"
            elif current_path.startswith("/api/index/"):
                environ["PATH_INFO"] = current_path[len("/api/index"):]
            elif current_path.startswith("/api/index.py/"):
                environ["PATH_INFO"] = current_path[len("/api/index.py"):]

        return self.wsgi_app(environ, start_response)


# Apply middleware to app
app.wsgi_app = VercelPathFixMiddleware(app.wsgi_app)

# Expose app for Vercel WSGI runner
if __name__ == "__main__":
    app.run()
