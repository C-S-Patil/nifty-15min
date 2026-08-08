import datetime
import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from kiteconnect import KiteConnect

API_KEY = os.getenv("KITE_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET", "YOUR_API_SECRET")
TOKEN_FILE = "access_token.json"


class AuthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        if "request_token" in query_components:
            request_token = query_components["request_token"][0]

            kite = KiteConnect(api_key=API_KEY)
            data = kite.generate_session(request_token, api_secret=API_SECRET)

            token_data = {
                "access_token": data["access_token"],
                "date": str(datetime.date.today()),
            }

            with open(TOKEN_FILE, "w") as f:
                json.dump(token_data, f)

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authentication Successful!</h1>")


def get_valid_kite_client():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            try:
                data = json.load(f)
                if data.get("date") == str(datetime.date.today()):
                    kite = KiteConnect(api_key=API_KEY)
                    kite.set_access_token(data["access_token"])
                    return kite
            except Exception:
                pass

    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    server = HTTPServer(("127.0.0.1", 8000), AuthHandler)
    webbrowser.open(login_url)

    server.handle_request()
    server.server_close()

    return get_valid_kite_client()