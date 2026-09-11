"""$batch support: multipart/mixed for V2, JSON for V4.

Changesets are atomic.  Before a changeset runs, the database is snapshotted
with SQLite's online backup API; if any request inside the changeset fails,
the snapshot is restored, matching SAP's all-or-nothing changeset semantics.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import List, Optional, Tuple

from .odata import SapError
from .service import Context, Response, dispatch

CRLF = b"\r\n"


class Part:
    def __init__(self, headers, body, boundary=None, parts=None):
        self.headers = headers          # dict of lower-cased header -> value
        self.body = body                # bytes
        self.boundary = boundary        # set for nested multipart parts
        self.parts = parts or []        # nested parts (changeset members)


def boundary_of(content_type: str) -> Optional[str]:
    if not content_type:
        return None
    m = re.search(r'boundary="?([^";]+)"?', content_type, re.I)
    return m.group(1).strip() if m else None


def split_parts(body: bytes, boundary: str) -> List[bytes]:
    delim = b"--" + boundary.encode("ascii")
    chunks = body.split(delim)
    out = []
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):  # closing delimiter
            break
        out.append(chunk.lstrip(b"\r\n"))
    return out


def parse_headers(raw: bytes) -> Tuple[dict, bytes]:
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    head, _, rest = raw.partition(sep)
    headers = {}
    for line in head.replace(b"\r\n", b"\n").split(b"\n"):
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().decode("latin-1").lower()] = v.strip().decode("latin-1")
    return headers, rest


def parse_part(raw: bytes) -> Part:
    headers, body = parse_headers(raw)
    ct = headers.get("content-type", "")
    if ct.lower().startswith("multipart/"):
        inner_boundary = boundary_of(ct)
        parts = [parse_part(p) for p in split_parts(body, inner_boundary)] if inner_boundary else []
        return Part(headers, body, inner_boundary, parts)
    return Part(headers, body)


_REQUEST_LINE = re.compile(rb"^([A-Z]+)\s+(\S+)\s+HTTP/[\d.]+\s*$")


def parse_http_request(raw: bytes, service_path: str):
    """Parse the `application/http` payload of one batch part."""
    raw = raw.lstrip(b"\r\n")
    line, _, rest = raw.partition(b"\n")
    m = _REQUEST_LINE.match(line.strip())
    if not m:
        raise SapError("Malformed request line in $batch part: %r" % line[:80], 400)
    method = m.group(1).decode("ascii")
    target = m.group(2).decode("utf-8")
    headers, body = parse_headers(rest)
    if not target.startswith("/"):
        if target.startswith("http://") or target.startswith("https://"):
            target = "/" + target.split("/", 3)[3]
        else:
            target = service_path.rstrip("/") + "/" + target.lstrip("/")
    path, _, query = target.partition("?")
    return method, path, query, headers, body


STATUS_TEXT = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    304: "Not Modified", 428: "Precondition Required",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    412: "Precondition Failed", 500: "Internal Server Error",
    501: "Not Implemented", 503: "Service Unavailable",
}


def render_response(resp: Response) -> bytes:
    out = [b"HTTP/1.1 %d %s" % (resp.status, STATUS_TEXT.get(resp.status, "Status").encode())]
    headers = dict(resp.headers)
    headers.setdefault("Content-Length", str(len(resp.body)))
    for key, value in headers.items():
        out.append(("%s: %s" % (key, value)).encode("latin-1"))
    return CRLF.join(out) + CRLF + CRLF + resp.body


def _snapshot(conn) -> Optional[sqlite3.Connection]:
    try:
        snap = sqlite3.connect(":memory:")
        conn.backup(snap)
        return snap
    except (AttributeError, sqlite3.Error):  # pragma: no cover - old SQLite
        return None


def _restore(conn, snap) -> None:
    if snap is not None:
        snap.backup(conn)


def handle_batch(ctx: Context, content_type: str, body: bytes,
                 service_path: str) -> Response:
    boundary = boundary_of(content_type)
    if not boundary:
        raise SapError(
            "The $batch request must use Content-Type multipart/mixed with a boundary", 400)

    resp_boundary = "batchresponse_" + str(uuid.uuid4())
    chunks: List[bytes] = []

    for raw in split_parts(body, boundary):
        part = parse_part(raw)
        if part.parts or (part.headers.get("content-type", "").lower().startswith("multipart/")):
            chunks.append(_run_changeset(ctx, part, service_path, resp_boundary))
        else:
            resp = _run_single(ctx, part, service_path)
            chunks.append(
                b"--" + resp_boundary.encode() + CRLF
                + b"Content-Type: application/http" + CRLF
                + b"Content-Transfer-Encoding: binary" + CRLF + CRLF
                + render_response(resp) + CRLF
            )

    payload = b"".join(chunks) + b"--" + resp_boundary.encode() + b"--" + CRLF
    return Response(
        202,
        headers={"DataServiceVersion": "1.0"},
        body=payload,
        content_type="multipart/mixed; boundary=%s" % resp_boundary,
    )


def _run_single(ctx: Context, part: Part, service_path: str) -> Response:
    try:
        method, path, query, headers, body = parse_http_request(part.body, service_path)
    except SapError as err:
        return Response.error(err)
    return dispatch(ctx, method, path, query, headers, body)


def _run_changeset(ctx: Context, part: Part, service_path: str, resp_boundary: str) -> bytes:
    cs_boundary = "changesetresponse_" + str(uuid.uuid4())
    snapshot = _snapshot(ctx.conn)
    rendered: List[bytes] = []
    failure: Optional[Response] = None

    for member in part.parts:
        resp = _run_single(ctx, member, service_path)
        if resp.status >= 400:
            failure = resp
            break
        rendered.append(
            b"--" + cs_boundary.encode() + CRLF
            + b"Content-Type: application/http" + CRLF
            + b"Content-Transfer-Encoding: binary" + CRLF + CRLF
            + render_response(resp) + CRLF
        )

    if failure is not None:
        # SAP rolls the whole changeset back and returns just the error.
        _restore(ctx.conn, snapshot)
        return (
            b"--" + resp_boundary.encode() + CRLF
            + b"Content-Type: application/http" + CRLF
            + b"Content-Transfer-Encoding: binary" + CRLF + CRLF
            + render_response(failure) + CRLF
        )

    inner = b"".join(rendered) + b"--" + cs_boundary.encode() + b"--" + CRLF
    return (
        b"--" + resp_boundary.encode() + CRLF
        + ("Content-Type: multipart/mixed; boundary=%s" % cs_boundary).encode() + CRLF
        + CRLF + inner
    )


# --------------------------------------------------------------------------
# JSON batch (OData V4)
# --------------------------------------------------------------------------


def handle_json_batch(ctx: Context, body: bytes, service_path: str) -> Response:
    """V4 sends a batch as JSON: {"requests": [...]} in, {"responses": [...]} out.

    Requests that share an `atomicityGroup` are the V4 spelling of a changeset,
    and are rolled back together if any of them fails.
    """
    try:
        payload = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise SapError("The $batch body is not valid JSON: %s" % exc, 400)
    requests = payload.get("requests")
    if not isinstance(requests, list):
        raise SapError('The $batch body must hold a "requests" array', 400)

    responses = []
    index = 0
    while index < len(requests):
        group = requests[index].get("atomicityGroup")
        if not group:
            responses.append(_run_json_request(ctx, requests[index], service_path))
            index += 1
            continue
        members = []
        while index < len(requests) and requests[index].get("atomicityGroup") == group:
            members.append(requests[index])
            index += 1
        responses.extend(_run_json_group(ctx, members, service_path))
    return Response(200, body={"responses": responses},
                    headers={"OData-Version": "4.0"})


def _run_json_request(ctx: Context, request: dict, service_path: str) -> dict:
    method = str(request.get("method", "GET")).upper()
    url = str(request.get("url", ""))
    if not url.startswith("/"):
        url = service_path.rstrip("/") + "/" + url.lstrip("/")
    path, _, query = url.partition("?")
    headers = {str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()}
    raw_body = request.get("body")
    if raw_body is None:
        encoded = b""
    elif isinstance(raw_body, (dict, list)):
        encoded = json.dumps(raw_body).encode("utf-8")
    else:
        encoded = str(raw_body).encode("utf-8")

    response = dispatch(ctx, method, path, query, headers, encoded)
    out = {
        "id": request.get("id"),
        "status": response.status,
        "headers": {k.lower(): v for k, v in response.headers.items()},
    }
    if request.get("atomicityGroup"):
        out["atomicityGroup"] = request["atomicityGroup"]
    if response.body:
        try:
            out["body"] = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            out["body"] = response.body.decode("utf-8", "replace")
    return out


def _run_json_group(ctx: Context, members: list, service_path: str) -> list:
    snapshot = _snapshot(ctx.conn)
    done = []
    for member in members:
        result = _run_json_request(ctx, member, service_path)
        done.append(result)
        if result["status"] >= 400:
            _restore(ctx.conn, snapshot)
            return [result]          # the group failed as a whole
    return done
