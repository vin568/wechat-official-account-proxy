"""HTTP proxy from Aliyun Function Compute to api.weixin.qq.com.

The function owns the WeChat credential, injects a stable access_token, and
forwards method, path, query, and body unchanged. Callers authenticate with
PROXY_KEY. Egress must leave through the VPC NAT EIP so the official-account
IP allowlist matches.
"""

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from hmac import compare_digest

UPSTREAM = "https://api.weixin.qq.com"
ALLOWED_PREFIXES = ("/cgi-bin/", "/wxa/")
BLOCKED_PATHS = frozenset({"/cgi-bin/token", "/cgi-bin/stable_token"})
DROP_REQUEST_HEADERS = frozenset({
    "authorization",
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "accept-encoding",  # keep error bodies plain so 40001/42001 can be retried
    "proxy-authorization",
    "proxy-connection",
})
DROP_RESPONSE_HEADERS = frozenset({
    "connection",
    "content-length",
    "date",
    "keep-alive",
    "server",
    "upgrade",
    "content-disposition",
    "transfer-encoding",
})
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")
TOKEN_REFRESH_CODES = frozenset({40001, 42001})
TOKEN = {"value": "", "exp": 0.0}
TOKEN_LOCK = threading.Lock()
LOGGER = logging.getLogger()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Ignore HTTP(S)_PROXY so traffic cannot skip the NAT EIP.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def handler(event, context):
    req = json.loads(event) if isinstance(event, (bytes, str)) else event
    request_id = (req.get("requestContext") or {}).get("requestId", "")
    headers = {str(k).lower(): str(v) for k, v in (req.get("headers") or {}).items()}

    auth = authorized(headers.get("authorization", ""))
    if auth is None:
        return respond(500, {"errcode": -1, "errmsg": "PROXY_KEY is not configured"})
    if not auth:
        return respond(401, {"errcode": -1, "errmsg": "unauthorized"})

    http = (req.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method", "")).upper()
    if method not in {"GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"}:
        return respond(405, {"errcode": -1, "errmsg": "method not allowed"})

    path = normalize_path(str(http.get("path", "")))
    if path is None:
        return respond(403, {"errcode": -1, "errmsg": "path not allowed"})

    query = {str(k): str(v) for k, v in (req.get("queryParameters") or {}).items()}
    body = decode_body(req.get("body") or "", req.get("isBase64Encoded") is True)
    content_type = headers.get("content-type", "")
    forward_headers = {
        k: v
        for k, v in headers.items()
        if k not in DROP_REQUEST_HEADERS and not k.startswith("x-fc-")
    }

    injected = False
    if not query.get("access_token"):
        query.pop("access_token", None)
        fetched, failure = access_token(False)
        if failure is not None:
            return failure
        query["access_token"] = fetched
        injected = True

    try:
        status, resp_headers, resp_body = upstream(
            method, path, query, body, content_type, forward_headers
        )
    except urllib.error.URLError:
        LOGGER.warning("upstream unreachable path=%s request_id=%s", path, request_id)
        return respond(502, {"errcode": -1, "errmsg": "upstream unreachable"})

    code = errcode(resp_body)
    if injected and code in TOKEN_REFRESH_CODES:
        fetched, failure = access_token(True)
        if failure is not None:
            return failure
        query["access_token"] = fetched
        try:
            status, resp_headers, resp_body = upstream(
                method, path, query, body, content_type, forward_headers
            )
        except urllib.error.URLError:
            LOGGER.warning("upstream unreachable path=%s request_id=%s", path, request_id)
            return respond(502, {"errcode": -1, "errmsg": "upstream unreachable"})
        code = errcode(resp_body)

    LOGGER.info(
        "proxied path=%s status=%s errcode=%s request_id=%s",
        path, status, code, request_id,
    )
    return respond_upstream(status, resp_headers, resp_body)


def authorized(value):
    expected = os.environ.get("PROXY_KEY", "")
    if not expected:
        return None
    scheme, _, given = value.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return compare_digest(given, expected)


def normalize_path(path):
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path.startswith("/") or any(ch in path for ch in "\\?#") or "://" in path:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts[1:]):
        return None
    normalized = "/" + "/".join(urllib.parse.quote(part, safe="") for part in parts[1:])
    if normalized in BLOCKED_PATHS or not normalized.startswith(ALLOWED_PREFIXES):
        return None
    return normalized


def decode_body(body, encoded):
    if not body:
        return b""
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    if not encoded:
        return raw
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return base64.b64decode(raw)


def access_token(force):
    now = time.time()
    with TOKEN_LOCK:
        if not force and TOKEN["value"] and now < TOKEN["exp"]:
            return TOKEN["value"], None
        appid = os.environ.get("WECHAT_APP_ID", "")
        secret = os.environ.get("WECHAT_APP_SECRET", "")
        if not appid or not secret:
            return None, respond(500, {"errcode": -1, "errmsg": "wechat credentials are not configured"})
        payload = json.dumps({
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
            "force_refresh": force,
        }).encode()
        req = urllib.request.Request(
            UPSTREAM + "/cgi-bin/stable_token",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with OPENER.open(req, timeout=10) as res:
                data = json.loads(res.read().decode())
                status = res.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                data = json.loads(exc.read().decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = {"errcode": -1, "errmsg": "stable_token failed"}
        except urllib.error.URLError:
            return None, respond(502, {"errcode": -1, "errmsg": "upstream unreachable"})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, respond(502, {"errcode": -1, "errmsg": "stable_token returned invalid json"})

        token = data.get("access_token")
        if not token:
            LOGGER.warning(
                "stable_token failed status=%s errcode=%s", status, data.get("errcode")
            )
            return None, respond(502, {
                "errcode": data.get("errcode", -1),
                "errmsg": data.get("errmsg", "stable_token failed"),
            })
        try:
            expires = int(data.get("expires_in", 7200))
        except (TypeError, ValueError):
            expires = 7200
        TOKEN["value"] = token
        TOKEN["exp"] = now + max(60, expires - 300)
        return token, None


def upstream(method, path, query, body, content_type, headers):
    url = UPSTREAM + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if method in ("GET", "HEAD") else body
    outbound = dict(headers)
    if data is not None and content_type:
        outbound["content-type"] = content_type
    req = urllib.request.Request(url, data=data, headers=outbound, method=method)
    try:
        with OPENER.open(req, timeout=50) as res:
            return res.status, dict(res.headers.items()), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def errcode(body):
    if not body.lstrip().startswith(b"{"):
        return None
    try:
        value = json.loads(body).get("errcode")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None
    return value if isinstance(value, int) else None


def respond_upstream(status, headers, body):
    lowered = {k.lower(): v for k, v in headers.items()}
    kept = {
        k: v
        for k, v in headers.items()
        if k.lower() not in DROP_RESPONSE_HEADERS and not k.lower().startswith("x-fc-")
    }
    if lowered.get("content-type", "").startswith(TEXT_TYPES):
        try:
            return {
                "statusCode": status,
                "headers": kept,
                "isBase64Encoded": False,
                "body": body.decode("utf-8"),
            }
        except UnicodeDecodeError:
            pass
    return {
        "statusCode": status,
        "headers": kept,
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode("ascii"),
    }


def respond(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(payload, ensure_ascii=False),
    }
