"""
DEPRECATED: This standalone OAuth script has been replaced by the in-app OAuth flow.
Use GET /auth/confluence in the running FastAPI server to authenticate.
Before first use, update the Atlassian developer console callback URL to:
  http://localhost:8000/auth/callback
This file is kept for reference only and is no longer maintained.

One-shot Atlassian OAuth 2.0 (3LO) flow.
Run this script, authorize in the browser, and it will print the access token.

Usage:
    python scripts/atlassian_oauth.py <client_id> <client_secret>
"""
import http.server
import json
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://localhost:9876/callback"
SCOPES = "read:page:confluence write:page:confluence read:space:confluence search:confluence offline_access"

_code = None
_server = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
        threading.Thread(target=_server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass


def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/atlassian_oauth.py <client_id> <client_secret>")
        sys.exit(1)

    client_id, client_secret = sys.argv[1], sys.argv[2]

    auth_url = (
        "https://auth.atlassian.com/authorize"
        f"?audience=api.atlassian.com"
        f"&client_id={client_id}"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&prompt=consent"
    )

    global _server
    _server = http.server.HTTPServer(("localhost", 9876), CallbackHandler)

    print(f"Opening browser for authorization...")
    webbrowser.open(auth_url)
    print("Waiting for callback on http://localhost:9876/callback ...")
    _server.serve_forever()

    if not _code:
        print("No code received.")
        sys.exit(1)

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _code,
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        "https://auth.atlassian.com/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")

    # Get accessible resources (cloud IDs)
    req2 = urllib.request.Request(
        "https://api.atlassian.com/oauth/token/accessible-resources",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req2) as resp:
        resources = json.loads(resp.read())

    dd_resource = next((r for r in resources if "datadoghq" in r.get("url", "")), resources[0] if resources else None)
    cloud_id = dd_resource["id"] if dd_resource else ""

    print("\n--- Add these to your .env ---")
    print(f"ATLASSIAN_CLOUD_ID={cloud_id}")
    print(f"ATLASSIAN_OAUTH_TOKEN={access_token}")
    if refresh_token:
        print(f"ATLASSIAN_REFRESH_TOKEN={refresh_token}")
    print("------------------------------")
    print(f"\nCloud ID: {cloud_id}")
    print(f"Site:     {dd_resource.get('url') if dd_resource else '?'}")


if __name__ == "__main__":
    main()
