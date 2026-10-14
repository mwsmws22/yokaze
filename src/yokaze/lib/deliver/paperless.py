"""Paperless-ngx HTTP helpers: upload, task→doc resolve, delete."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from uuid import uuid4

from yokaze.lib.util.errors import DeliverFailedError


def _env_url_token() -> tuple[str, str]:
    url = os.environ.get("PAPERLESS_URL", "").rstrip("/")
    token = os.environ.get("PAPERLESS_TOKEN", "")
    if not url or not token:
        raise DeliverFailedError(
            "Error: PAPERLESS_URL and PAPERLESS_TOKEN required for --target paperless"
        )
    return url, token


def _ssl_context() -> ssl.SSLContext:
    # Match legacy `curl -sk` for self-signed Paperless installs.
    return ssl._create_unverified_context()


def _request(
    method: str,
    url: str,
    *,
    token: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> tuple[int, str]:
    hdrs = {"Authorization": f"Token {token}"}
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(
            request, context=_ssl_context(), timeout=timeout
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            return int(status), body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        msg = f"Paperless HTTP {exc.code}"
        if detail:
            msg = f"{msg}: {detail}"
        raise DeliverFailedError(msg) from exc
    except urllib.error.URLError as exc:
        raise DeliverFailedError(f"Paperless request failed: {exc.reason}") from exc


def post_document(pdf: Path, *, name: str) -> str:
    """Upload PDF; return consume **task UUID** (not document id)."""
    base, token = _env_url_token()
    endpoint = f"{base}/api/documents/post_document/"
    boundary = f"----yokaze-{uuid4().hex}"
    body = _multipart_body(
        boundary,
        fields={"created": date.today().isoformat()},
        files={"document": (pdf.name, pdf.read_bytes(), "application/pdf")},
    )
    status, text = _request(
        "POST",
        endpoint,
        token=token,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    if status != 200:
        raise DeliverFailedError(f"Upload failed (HTTP {status})")
    task_uuid = _parse_task_uuid(text)
    if not task_uuid:
        raise DeliverFailedError(f"Upload OK but no task UUID in response: {text!r}")
    print(f"Done: {name}.pdf uploaded to Paperless (task {task_uuid})")
    return task_uuid


def resolve_document_id(task_uuid: str) -> int:
    """Map consume task UUID → document id. Raises if not ready / failed."""
    base, token = _env_url_token()
    query = urllib.parse.urlencode({"task_id": task_uuid})
    status, text = _request(
        "GET", f"{base}/api/tasks/?{query}", token=token, timeout=30
    )
    if status != 200:
        raise DeliverFailedError(f"Task lookup failed (HTTP {status})")
    doc_id = _parse_related_document(text, task_uuid)
    if doc_id is None:
        raise DeliverFailedError(
            f"Paperless task {task_uuid} has no document id yet "
            "(still processing, failed, or missing)"
        )
    return doc_id


def document_already_gone(exc: BaseException) -> bool:
    """True when Paperless has no document for this replace (already deleted)."""
    if not isinstance(exc, DeliverFailedError):
        return False
    text = str(exc)
    return "HTTP 404" in text


def require_document(document_id: int) -> None:
    """Raise when Paperless has no document for this id (GET, no delete)."""
    base, token = _env_url_token()
    status, _text = _request(
        "GET",
        f"{base}/api/documents/{int(document_id)}/",
        token=token,
        timeout=30,
    )
    if status == 404:
        raise DeliverFailedError(
            'Paperless HTTP 404: {"detail":"No Document matches the given query."}'
        )
    if status != 200:
        raise DeliverFailedError(
            f"Document lookup failed (HTTP {status}) for document {document_id}"
        )


def resolve_replace_document_ids(
    task_uuids: list[str],
) -> tuple[list[int], list[str]]:
    """Resolve Restage UUIDs and check the document still exists.

    A consume task can still name a document that was deleted in Paperless.
    Those UUIDs go in ``missing`` so Stage can ask before upload.
    """
    found: list[int] = []
    missing: list[str] = []
    for task_uuid in task_uuids:
        try:
            doc_id = resolve_document_id(task_uuid)
            require_document(doc_id)
            found.append(doc_id)
        except Exception as exc:  # noqa: BLE001 — classify gone vs still failing
            if document_already_gone(exc):
                missing.append(task_uuid)
                continue
            raise
    return found, missing


def delete_replace_documents(document_ids: list[int]) -> None:
    """Delete resolved docs that were checked present. A late 404 is ignored."""
    for document_id in document_ids:
        try:
            delete_document(document_id)
        except Exception as exc:  # noqa: BLE001
            if document_already_gone(exc):
                continue
            raise


def delete_document(document_id: int) -> None:
    base, token = _env_url_token()
    status, _text = _request(
        "DELETE", f"{base}/api/documents/{int(document_id)}/", token=token, timeout=60
    )
    if status not in (200, 204):
        raise DeliverFailedError(f"Delete document {document_id} failed (HTTP {status})")


def _parse_task_uuid(body: str) -> str | None:
    text = (body or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Bare uuid string without JSON quotes
        return text.strip('"') or None
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in ("task_id", "id", "uuid"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _parse_related_document(body: str, task_uuid: str) -> int | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeliverFailedError(f"corrupt tasks response: {body[:200]!r}") from exc

    rows: list[dict] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            rows = [r for r in results if isinstance(r, dict)]
        else:
            rows = [data]

    for row in rows:
        row_task = str(row.get("task_id") or row.get("id") or "")
        if row_task and row_task != task_uuid:
            continue
        status = str(row.get("status") or "").upper()
        related = row.get("related_document")
        if related is not None and str(related).strip().isdigit():
            return int(related)
        if status in {"SUCCESS", "SUCCESSFUL"}:
            # Some versions embed id only in result text.
            result = str(row.get("result") or "")
            for token in result.replace(",", " ").split():
                if token.isdigit():
                    return int(token)
        if status in {"FAILURE", "FAILED"}:
            raise DeliverFailedError(
                f"Paperless task {task_uuid} failed: {row.get('result') or status}"
            )
    return None


def _multipart_body(
    boundary: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for key, (filename, data, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)
