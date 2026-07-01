"""DingPan backup helpers for leaveAdmin.

Creates a consistent SQLite backup package and uploads it through an abstract
DingPan client. The real DingTalk client is intentionally kept behind an
interface so tests never call external APIs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover - urllib fallback covers production hosts without requests
    requests = None


class UrlLibResponse:
    def __init__(self, status_code: int, body: bytes, url: str):
        self.status_code = status_code
        self._body = body
        self.url = url
        self.text = body.decode("utf-8", errors="replace")

    def json(self) -> Dict[str, Any]:
        return json.loads(self.text or "{}")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise BackupError(f"HTTP {self.status_code} from {self.url}: {self.text[:300]}")


class UrlLibSession:
    """Tiny requests-compatible fallback for hosts without requests installed."""

    @staticmethod
    def _request(method: str, url: str, **kwargs: Any) -> UrlLibResponse:
        import urllib.parse
        import urllib.request
        import urllib.error

        params = kwargs.get("params")
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        headers = dict(kwargs.get("headers") or {})
        data = kwargs.get("data")
        json_body = kwargs.get("json")
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif hasattr(data, "read"):
            data = data.read()
        timeout = kwargs.get("timeout")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return UrlLibResponse(resp.status, resp.read(), url)
        except urllib.error.HTTPError as exc:
            return UrlLibResponse(exc.code, exc.read(), url)

    def post(self, url: str, **kwargs: Any) -> UrlLibResponse:
        return self._request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> UrlLibResponse:
        return self._request("GET", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> UrlLibResponse:
        return self._request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> UrlLibResponse:
        return self._request("DELETE", url, **kwargs)

BACKUP_VERSION = 1
DEFAULT_PREFIX = "leaveAdmin-backup-"
DEFAULT_SPACE_NAME = "leaveadmin-backup"


class BackupError(RuntimeError):
    """Raised when a backup operation fails."""


class DingpanClient:
    """Abstract DingPan client.

    Implementations should wrap DingTalk Drive APIs:
    createSpace/getUploadInfo/OSS upload/addFile/addPermission/list/delete.
    """

    def ensure_space(self, space_name: str, union_id: str | None = None) -> str:
        raise NotImplementedError

    def get_upload_info(self, space_id: str, parent_id: str, file_name: str, file_size: int, md5: str) -> Dict[str, Any]:
        raise NotImplementedError

    def oss_upload(self, upload_info: Dict[str, Any], local_path: Path) -> None:
        raise NotImplementedError

    def add_file(self, space_id: str, parent_id: str, file_name: str, media_id: str, union_id: str | None = None) -> Dict[str, Any]:
        raise NotImplementedError

    def add_permission(self, space_id: str, file_id: str, user_id: str | None = None, corp_id: str | None = None, union_id: str | None = None) -> None:
        raise NotImplementedError

    def list_files(self, space_id: str, parent_id: str = "0") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def delete_file(self, space_id: str, file_id: str) -> None:
        raise NotImplementedError

    def get_download_info(self, space_id: str, file_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def download_file(self, space_id: str, file_id: str, dest_path: str | Path) -> Path:
        raise NotImplementedError

    def download_latest_backup(self, space_id: str, dest_dir: str | Path, name_prefix: str | None = None) -> Dict[str, Any]:
        raise NotImplementedError


class DingTalkDriveClient(DingpanClient):
    """DingTalk Drive client using visible shared-space APIs.

    Requires a DingTalk Drive shared space. For this DR path we intentionally use
    the visible Drive shared-space API (`/v1.0/drive/spaces`) rather than the newer
    storage-space API: storage spaces are not visible in the DingTalk client, which
    defeats the manual-download failover requirement.
    """

    API_BASE = "https://api.dingtalk.com"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        union_id: str,
        space_id: str | None = None,
        root_dentry_uuid: str | None = None,
        session: Any = None,
        timeout: int = 30,
    ):
        # Use requests when available; otherwise fall back to urllib so the cloud
        # host does not need an extra package just to run the hourly backup.
        self.app_key = app_key
        self.app_secret = app_secret
        self.union_id = union_id
        self.space_id = space_id
        # Kept only for backward-compatible config files. Drive shared spaces use
        # parentId (root = "0"), not rootDentryUuid.
        self.root_dentry_uuid = root_dentry_uuid
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:
            self.session = UrlLibSession()
        self.timeout = timeout
        self._access_token: str | None = None
        self.last_created_space: Dict[str, Any] | None = None

    def _json(self, response: Any) -> Dict[str, Any]:
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        try:
            data = response.json()
        except Exception as exc:
            raise BackupError(f"DingTalk response is not JSON: {getattr(response, 'text', '')[:300]}") from exc
        if isinstance(data, dict) and data.get("code") and str(data.get("code")) not in ("0", "OK"):
            raise BackupError(f"DingTalk API error: {data}")
        return data

    def get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        response = self.session.post(
            f"{self.API_BASE}/v1.0/oauth2/accessToken",
            json={"appKey": self.app_key, "appSecret": self.app_secret},
            timeout=self.timeout,
        )
        data = self._json(response)
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise BackupError(f"accessToken missing from response: {data}")
        self._access_token = token
        return token

    def _headers(self) -> Dict[str, str]:
        return {"x-acs-dingtalk-access-token": self.get_access_token(), "Content-Type": "application/json"}

    def ensure_space(self, space_name: str, union_id: str | None = None) -> str:
        if self.space_id:
            return self.space_id
        response = self.session.post(
            f"{self.API_BASE}/v1.0/drive/spaces",
            headers=self._headers(),
            json={"name": space_name, "unionId": union_id or self.union_id},
            timeout=self.timeout,
        )
        data = self._json(response)
        space_id = data.get("spaceId") or data.get("id")
        if not space_id:
            raise BackupError(f"create DingPan space response missing spaceId: {data}")
        self.space_id = space_id
        self.last_created_space = data
        return space_id

    def get_upload_info(self, space_id: str, parent_id: str, file_name: str, file_size: int, md5: str) -> Dict[str, Any]:
        parent = parent_id or "0"
        response = self.session.post(
            f"{self.API_BASE}/v1.0/storage/spaces/{space_id}/files/uploadInfos/query",
            params={"unionId": self.union_id},
            headers=self._headers(),
            json={
                "protocol": "HEADER_SIGNATURE",
                "multipart": False,
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {
                        "md5": md5,
                        "size": file_size,
                        "parentId": parent,
                        "name": file_name,
                    },
                    "preferRegion": "ZHANGJIAKOU",
                    "preferIntranet": False,
                },
            },
            timeout=self.timeout,
        )
        data = self._json(response)
        upload_obj = (
            data.get("headerSignatureUploadInfo")
            or data.get("headerSignatureInfo")
            or data.get("stsUploadInfo")
            or data
        )
        media_id = upload_obj.get("mediaId") or upload_obj.get("media_id") or data.get("mediaId") or data.get("uploadKey")
        if not media_id:
            raise BackupError(f"upload info missing mediaId/uploadKey: {data}")
        result = dict(data)
        result["media_id"] = media_id
        return result

    def oss_upload(self, upload_info: Dict[str, Any], local_path: Path) -> None:
        header_info = (
            upload_info.get("headerSignatureUploadInfo")
            or upload_info.get("headerSignatureInfo")
            or {}
        )
        urls = header_info.get("resourceUrls") or header_info.get("resourceUrl") or []
        if isinstance(urls, str):
            urls = [urls]
        headers = dict(header_info.get("headers") or {})
        # DingTalk/OSS header signatures are sensitive to Content-Type. urllib
        # adds application/x-www-form-urlencoded by default for data=..., which
        # breaks the signature. Force an empty Content-Type unless DingTalk signed
        # a concrete one.
        headers.setdefault("Content-Type", "")
        if not urls:
            # The drive API may return stsUploadInfo, which requires OSS SDK style
            # upload. Keep this explicit so production probes fail loudly instead
            # of pretending a file was uploaded.
            raise BackupError("upload info has no HEADER_SIGNATURE resourceUrls; OSS STS upload is not implemented")
        with open(local_path, "rb") as handle:
            response = self.session.put(urls[0], headers=headers, data=handle, timeout=self.timeout)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()

    def add_file(self, space_id: str, parent_id: str, file_name: str, media_id: str, union_id: str | None = None) -> Dict[str, Any]:
        parent = parent_id or "0"
        # Do NOT use the legacy Drive metadata API here. Official docs now mark
        # /v1.0/drive/spaces/{spaceId}/files as historical and not grantable for
        # new permission applications. The storage-space commit API accepts the
        # uploadKey returned by uploadInfos/query and creates a visible dentry in
        # the same Drive shared space.
        response = self.session.post(
            f"{self.API_BASE}/v1.0/storage/spaces/{space_id}/files/commit",
            params={"unionId": union_id or self.union_id},
            headers=self._headers(),
            json={
                "uploadKey": media_id,
                "name": file_name,
                "parentId": parent,
                "option": {
                    "conflictStrategy": "AUTO_RENAME",
                    "convertToOnlineDoc": False,
                },
            },
            timeout=self.timeout,
        )
        data = self._json(response)
        dentry = data.get("dentry") or data
        file_id = dentry.get("fileId") or dentry.get("id") or dentry.get("dentryUuid") or dentry.get("uuid")
        if not file_id:
            raise BackupError(f"commit file response missing fileId/dentry id: {data}")
        return {
            "file_id": file_id,
            "name": dentry.get("fileName") or dentry.get("name") or file_name,
            "space_id": dentry.get("spaceId") or space_id,
            "modified_time": dentry.get("modifyTime") or dentry.get("modifiedTime") or dentry.get("createTime") or time.time(),
        }

    def add_permission(self, space_id: str, file_id: str, user_id: str | None = None, corp_id: str | None = None, union_id: str | None = None) -> None:
        # A file created by this app/user is already reachable by the owner.
        # Add permission only when caller provides a concrete user/corp pair.
        if not user_id or not corp_id:
            return
        response = self.session.post(
            f"{self.API_BASE}/v2.0/storage/spaces/{space_id}/dentries/{file_id}/permissions",
            params={"unionId": union_id or self.union_id},
            headers=self._headers(),
            json={
                "role": "viewer",
                "members": [{"corpId": corp_id, "memberType": "user", "memberId": user_id}],
            },
            timeout=self.timeout,
        )
        self._json(response)

    def list_files(self, space_id: str, parent_id: str = "0") -> List[Dict[str, Any]]:
        # Current DingTalk docs expose file discovery through the storage search
        # API. The old Drive list endpoint (/v1.0/drive/spaces/{spaceId}/files)
        # requires a Drive file permission that is not present in this tenant even
        # when Drive.Space.* permissions are granted.
        response = self.session.post(
            f"{self.API_BASE}/v2.0/storage/dentries/search",
            params={"operatorId": self.union_id},
            headers=self._headers(),
            json={"keyword": DEFAULT_PREFIX, "option": {"maxResults": 50}},
            timeout=self.timeout,
        )
        data = self._json(response)
        items = data.get("items") or []
        result = []
        for item in items:
            dentry_uuid = item.get("dentryUuid") or item.get("uuid")
            dentry_id = item.get("dentryId") or item.get("id")
            result.append({
                "file_id": item.get("fileId") or dentry_id or dentry_uuid,
                "dentry_id": dentry_id,
                "dentry_uuid": dentry_uuid,
                "name": item.get("fileName") or item.get("name"),
                "modified_time": item.get("modifyTime") or item.get("modifiedTime") or item.get("createTime") or 0,
            })
        return result

    def delete_file(self, space_id: str, file_id: str) -> None:
        response = self.session.delete(
            f"{self.API_BASE}/v1.0/drive/spaces/{space_id}/files/{file_id}",
            params={"unionId": self.union_id},
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._json(response)

    def query_dentry_id_by_uuid(self, dentry_uuid: str) -> Dict[str, Any]:
        """Map Storage search dentryUuid to numeric dentryId for downloadInfos.

        DingTalk's storage search returns dentryUuid, while the storage download
        info API expects the numeric dentryId. The documented bridge is the doc
        mapping endpoint.
        """
        response = self.session.get(
            f"{self.API_BASE}/v2.0/doc/dentries/{dentry_uuid}/queryDentryId",
            params={"operatorId": self.union_id},
            headers=self._headers(),
            timeout=self.timeout,
        )
        data = self._json(response)
        dentry_id = data.get("dentryId") or data.get("id")
        if not dentry_id:
            raise BackupError(f"queryDentryId response missing dentryId: {data}")
        return data

    def get_download_info(self, space_id: str, file_id: str) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.API_BASE}/v1.0/storage/spaces/{space_id}/dentries/{file_id}/downloadInfos/query",
            params={"unionId": self.union_id},
            headers=self._headers(),
            json={"option": {"version": 1, "preferIntranet": False}},
            timeout=self.timeout,
        )
        data = self._json(response)
        header_info = data.get("headerSignatureInfo") or data.get("headerSignatureDownloadInfo") or {}
        urls = header_info.get("resourceUrls") or header_info.get("resourceUrl") or []
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            raise BackupError(f"download info missing HEADER_SIGNATURE resourceUrls: {data}")
        return data

    def download_file(self, space_id: str, file_id: str, dest_path: str | Path) -> Path:
        info = self.get_download_info(space_id, file_id)
        header_info = info.get("headerSignatureInfo") or info.get("headerSignatureDownloadInfo") or {}
        urls = header_info.get("resourceUrls") or header_info.get("resourceUrl") or []
        if isinstance(urls, str):
            urls = [urls]
        headers = dict(header_info.get("headers") or {})
        response = self.session.get(urls[0], headers=headers, timeout=self.timeout)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        if hasattr(response, "content"):
            body = response.content
        elif hasattr(response, "_body"):
            body = response._body
        else:
            body = str(getattr(response, "text", "")).encode("utf-8")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest

    def download_latest_backup(self, space_id: str, dest_dir: str | Path, name_prefix: str | None = None) -> Dict[str, Any]:
        prefix = name_prefix or DEFAULT_PREFIX
        files = [
            item for item in self.list_files(space_id)
            if _is_backup_file(str(item.get("name") or item.get("file_name") or ""), name_prefix=prefix)
        ]
        if not files:
            raise BackupError(f"no DingPan backup file found with prefix {prefix}")
        files.sort(key=_file_time, reverse=True)
        latest = files[0]
        file_id = latest.get("dentry_id") or latest.get("file_id") or latest.get("fileId") or latest.get("dentryUuid") or latest.get("uuid")
        dentry_uuid = latest.get("dentry_uuid") or latest.get("dentryUuid") or latest.get("uuid")
        name = str(latest.get("name") or latest.get("file_name") or f"{prefix}latest.tar.gz")
        if not file_id and not dentry_uuid:
            raise BackupError(f"latest backup missing file id: {latest}")
        # Storage search commonly returns dentryUuid, but downloadInfos expects
        # the numeric dentryId. Resolve it only for UUID-shaped/non-numeric ids so
        # direct numeric dentryId callers still work.
        if dentry_uuid and (not file_id or not str(file_id).isdigit()):
            mapping = self.query_dentry_id_by_uuid(str(dentry_uuid))
            file_id = mapping.get("dentryId") or mapping.get("id")
        if not file_id:
            raise BackupError(f"latest backup missing numeric dentryId: {latest}")
        dest = Path(dest_dir) / Path(name).name
        self.download_file(space_id, str(file_id), dest)
        return {"path": dest, "name": name, "file_id": str(file_id), "file_info": latest}


class FakeDingpanClient(DingpanClient):
    """In-memory fake for tests."""

    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.space_id = "fake_space"
        self.files: List[Dict[str, Any]] = []
        self.calls = {
            "ensure_space": 0,
            "get_upload_info": 0,
            "oss_upload": 0,
            "add_file": 0,
            "add_permission": 0,
            "list_files": 0,
            "delete_file": 0,
        }

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise BackupError(f"fake failure on {name}")

    def ensure_space(self, space_name: str, union_id: str | None = None) -> str:
        self.calls["ensure_space"] += 1
        self._maybe_fail("ensure_space")
        return self.space_id

    def get_upload_info(self, space_id: str, parent_id: str, file_name: str, file_size: int, md5: str) -> Dict[str, Any]:
        self.calls["get_upload_info"] += 1
        self._maybe_fail("get_upload_info")
        return {"media_id": f"media_{len(self.files) + 1}", "file_name": file_name, "file_size": file_size, "md5": md5}

    def oss_upload(self, upload_info: Dict[str, Any], local_path: Path) -> None:
        self.calls["oss_upload"] += 1
        self._maybe_fail("oss_upload")
        if not Path(local_path).exists():
            raise BackupError(f"upload source missing: {local_path}")

    def add_file(self, space_id: str, parent_id: str, file_name: str, media_id: str, union_id: str | None = None) -> Dict[str, Any]:
        self.calls["add_file"] += 1
        self._maybe_fail("add_file")
        record = {
            "file_id": f"file_{len(self.files) + 1}",
            "name": file_name,
            "file_name": file_name,
            "modified_time": int(time.time()),
            "media_id": media_id,
            "space_id": space_id,
        }
        self.files.append(record)
        return record

    def add_permission(self, space_id: str, file_id: str, user_id: str | None = None, corp_id: str | None = None, union_id: str | None = None) -> None:
        self.calls["add_permission"] += 1
        self._maybe_fail("add_permission")

    def list_files(self, space_id: str, parent_id: str = "0") -> List[Dict[str, Any]]:
        self.calls["list_files"] += 1
        self._maybe_fail("list_files")
        return list(self.files)

    def delete_file(self, space_id: str, file_id: str) -> None:
        self.calls["delete_file"] += 1
        self._maybe_fail("delete_file")
        self.files = [f for f in self.files if f.get("file_id") != file_id]

    def add_existing_file(self, name: str, modified_time: int, file_id: str | None = None, content: bytes | None = None) -> None:
        file_id = file_id or f"existing_{len(self.files)}"
        self.files.append({"name": name, "file_name": name, "modified_time": modified_time, "file_id": file_id})
        if not hasattr(self, "_download_bodies"):
            self._download_bodies = {}
        self._download_bodies[file_id] = content if content is not None else name.encode("utf-8")

    def get_download_info(self, space_id: str, file_id: str) -> Dict[str, Any]:
        self.calls.setdefault("get_download_info", 0)
        self.calls["get_download_info"] += 1
        self._maybe_fail("get_download_info")
        return {"file_id": file_id, "space_id": space_id, "fake": True}

    def download_file(self, space_id: str, file_id: str, dest_path: str | Path) -> Path:
        self.calls.setdefault("download_file", 0)
        self.calls["download_file"] += 1
        self._maybe_fail("download_file")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = getattr(self, "_download_bodies", {}).get(file_id, file_id.encode("utf-8"))
        dest.write_bytes(body)
        return dest

    def download_latest_backup(self, space_id: str, dest_dir: str | Path, name_prefix: str | None = None) -> Dict[str, Any]:
        prefix = name_prefix or DEFAULT_PREFIX
        files = [
            item for item in self.list_files(space_id)
            if _is_backup_file(str(item.get("name") or item.get("file_name") or ""), name_prefix=prefix)
        ]
        if not files:
            raise BackupError(f"no DingPan backup file found with prefix {prefix}")
        files.sort(key=_file_time, reverse=True)
        latest = files[0]
        file_id = latest.get("file_id") or latest.get("fileId")
        name = str(latest.get("name") or latest.get("file_name") or f"{prefix}latest.tar.gz")
        dest = Path(dest_dir) / Path(name).name
        self.download_file(space_id, str(file_id), dest)
        return {"path": dest, "name": name, "file_id": str(file_id), "file_info": latest}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_online_backup(db_path: Path, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _scalar(conn: sqlite3.Connection, sql: str, default: Any = 0) -> Any:
    try:
        row = conn.execute(sql).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]
    except sqlite3.Error:
        return default


def build_manifest(db_path: Path, source_host: str | None = None) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        period = _scalar(conn, "SELECT MAX(period) FROM leave_balances", dt.datetime.now().strftime("%Y-%m"))
        employees_active = _scalar(conn, "SELECT COUNT(*) FROM employees WHERE COALESCE(is_disabled,0)=0", 0)
        leave_balance_rows = _scalar(conn, "SELECT COUNT(*) FROM leave_balances", 0)
        latest_transaction_id = _scalar(conn, "SELECT MAX(id) FROM balance_transactions", 0)
    finally:
        conn.close()
    return {
        "service": "leaveAdmin",
        "backup_time": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "period": period,
        "db_file": "admin.db",
        "db_size": db_path.stat().st_size,
        "db_sha256": sha256_file(db_path),
        "employees_active": employees_active,
        "leave_balance_rows": leave_balance_rows,
        "latest_transaction_id": latest_transaction_id,
        "source_host": source_host or socket.gethostname(),
        "version": BACKUP_VERSION,
    }


def create_backup_package(db_path: str | Path, out_dir: str | Path, source_host: str | None = None, name_prefix: str = DEFAULT_PREFIX) -> Dict[str, Any]:
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    if not db_path.exists():
        raise BackupError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    package_name = f"{name_prefix}{stamp}.tar.gz"
    package_path = out_dir / package_name

    with tempfile.TemporaryDirectory(prefix="leaveadmin-backup-") as tmp:
        tmp_dir = Path(tmp)
        backup_db = tmp_dir / "admin.db"
        manifest_path = tmp_dir / "manifest.json"
        sqlite_online_backup(db_path, backup_db)
        manifest = build_manifest(backup_db, source_host=source_host)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with tarfile.open(package_path, "w:gz") as tar:
            tar.add(backup_db, arcname="admin.db")
            tar.add(manifest_path, arcname="manifest.json")
    return {"path": package_path, "manifest": manifest, "name": package_name}


def extract_backup_package(package_path: str | Path, extract_dir: str | Path) -> Dict[str, Any]:
    package_path = Path(package_path)
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package_path, "r:gz") as tar:
        members = tar.getmembers()
        allowed = {"admin.db", "manifest.json"}
        for member in members:
            if member.name not in allowed:
                raise BackupError(f"unexpected file in backup package: {member.name}")
        try:
            tar.extractall(extract_dir, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12 compatibility
            tar.extractall(extract_dir)
    manifest_path = extract_dir / "manifest.json"
    db_path = extract_dir / "admin.db"
    if not manifest_path.exists() or not db_path.exists():
        raise BackupError("backup package missing admin.db or manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"db_path": db_path, "manifest_path": manifest_path, "manifest": manifest}


def verify_backup_package(package_path: str | Path) -> Dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(prefix="leaveadmin-verify-") as tmp:
            extracted = extract_backup_package(package_path, tmp)
            manifest = extracted["manifest"]
            ok = manifest.get("db_sha256") == sha256_file(extracted["db_path"])
            return {"ok": ok, "manifest": manifest, "db_path": extracted["db_path"]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "manifest": {}}


def _file_time(file_info: Dict[str, Any]) -> float:
    for key in ("modified_time", "modifiedTime", "updated_at", "created_at", "time"):
        value = file_info.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            if float(value) > 0:
                return float(value)
            continue
        if isinstance(value, str):
            try:
                return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    name = str(file_info.get("name") or file_info.get("file_name") or "")
    # Fallback for storage search results, which may omit timestamps but backup
    # filenames are stable: leaveAdmin-backup-YYYYMMDD-HHMMSS.tar.gz.
    marker = "backup-"
    if marker in name:
        stamp = name.split(marker, 1)[1][:15]
        try:
            return dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            pass
    return 0.0


def _is_backup_file(name: str, name_prefix: str | None = None) -> bool:
    if name_prefix:
        return name.startswith(name_prefix)
    return name.endswith(".tar.gz") and "backup" in name.lower()


def select_files_to_delete(files: Iterable[Dict[str, Any]], keep: int = 24, name_prefix: str | None = None) -> List[Dict[str, Any]]:
    backups = [
        f for f in files
        if _is_backup_file(str(f.get("name") or f.get("file_name") or ""), name_prefix=name_prefix)
    ]
    backups.sort(key=_file_time, reverse=True)
    return backups[keep:]


def run_backup(
    db_path: str | Path,
    out_dir: str | Path,
    client: DingpanClient,
    space_name: str = DEFAULT_SPACE_NAME,
    keep: int = 24,
    dry_run: bool = False,
    notify: Optional[Callable[[str, str], None]] = None,
    union_id: str | None = None,
    parent_id: str = "0",
    viewer_user_id: str | None = None,
    corp_id: str | None = None,
    name_prefix: str | None = None,
    source_host: str | None = None,
    log_path: str | Path | None = None,
) -> Dict[str, Any]:
    try:
        package_prefix = name_prefix or DEFAULT_PREFIX
        package = create_backup_package(db_path, out_dir, source_host=source_host, name_prefix=package_prefix)
        package_path = Path(package["path"])
        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "uploaded": False,
            "package_path": str(package_path),
            "manifest": package["manifest"],
            "pruned": 0,
        }
        if dry_run:
            return report

        space_id = client.ensure_space(space_name, union_id=union_id)
        upload_info = client.get_upload_info(space_id, parent_id, package_path.name, package_path.stat().st_size, md5_file(package_path))
        client.oss_upload(upload_info, package_path)
        media_id = upload_info.get("media_id") or upload_info.get("mediaId") or upload_info.get("media_id_str")
        if not media_id:
            raise BackupError("upload info missing media_id")
        file_info = client.add_file(space_id, parent_id, package_path.name, media_id, union_id=union_id)
        file_id = file_info.get("file_id") or file_info.get("fileId")
        if not file_id:
            raise BackupError("add_file response missing file_id")
        client.add_permission(space_id, file_id, user_id=viewer_user_id, corp_id=corp_id, union_id=union_id)
        record = {
            "file_id": file_id,
            "space_id": space_id,
            "file_name": package_path.name,
            "sha256": sha256_file(package_path),
            "size": package_path.stat().st_size,
            "time": dt.datetime.now().isoformat(timespec="seconds"),
        }
        report.update({"uploaded": True, "record": record})
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            files = client.list_files(space_id, parent_id)
            to_delete = select_files_to_delete(files, keep=keep, name_prefix=name_prefix)
            for item in to_delete:
                old_file_id = item.get("file_id") or item.get("fileId")
                if old_file_id and old_file_id != file_id:
                    client.delete_file(space_id, old_file_id)
                    report["pruned"] += 1
        except Exception as prune_exc:
            # Retention cleanup is important, but it must not turn a successful DR
            # upload into a reported upload failure. Some tenants grant write
            # before list/delete permissions; keep the fresh backup and surface a
            # warning for follow-up.
            report["prune_error"] = str(prune_exc)
            if notify:
                notify("钉盘备份清理失败", f"备份已上传成功，但清理旧备份失败：{prune_exc}")
        return report
    except Exception as exc:
        if notify:
            notify("钉盘备份失败", str(exc))
        if isinstance(exc, BackupError):
            raise
        raise BackupError(str(exc)) from exc


def load_json_config(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_client_from_configs(app_config_path: str | Path, dingpan_config_path: str | Path, session: Any = None) -> tuple[DingTalkDriveClient, Dict[str, Any]]:
    app_config = load_json_config(app_config_path)
    dingpan_config = load_json_config(dingpan_config_path)
    app_key = app_config.get("appKey") or app_config.get("app_key")
    app_secret = app_config.get("appSecret") or app_config.get("app_secret")
    union_id = dingpan_config.get("unionId") or dingpan_config.get("union_id")
    if not app_key or not app_secret:
        raise BackupError("config.json missing appKey/appSecret")
    if not union_id:
        raise BackupError("dingpan_backup_config.json missing unionId")
    client = DingTalkDriveClient(
        app_key=app_key,
        app_secret=app_secret,
        union_id=union_id,
        space_id=dingpan_config.get("spaceId") or dingpan_config.get("space_id"),
        root_dentry_uuid=dingpan_config.get("rootDentryUuid") or dingpan_config.get("root_dentry_uuid"),
        session=session,
        timeout=int(dingpan_config.get("timeout", 30)),
    )
    runtime = {
        "union_id": union_id,
        "space_name": dingpan_config.get("spaceName") or dingpan_config.get("space_name") or DEFAULT_SPACE_NAME,
        "parent_id": dingpan_config.get("parentId") or dingpan_config.get("parent_id") or dingpan_config.get("rootDentryUuid") or dingpan_config.get("root_dentry_uuid") or "0",
        "viewer_user_id": dingpan_config.get("viewerUserId") or dingpan_config.get("viewer_user_id"),
        "corp_id": dingpan_config.get("corpId") or dingpan_config.get("corp_id"),
        "keep": int(dingpan_config.get("keep", 24)),
        "log_path": dingpan_config.get("logPath") or dingpan_config.get("log_path"),
    }
    return client, runtime


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="leaveAdmin 钉盘备份工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    upload = sub.add_parser("upload")
    upload.add_argument("--db", default="admin.db")
    upload.add_argument("--out-dir", default="backups/dingpan_hourly")
    upload.add_argument("--app-config", default="config.json")
    upload.add_argument("--dingpan-config", required=True)
    upload.add_argument("--keep", type=int)
    upload.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "upload":
        client, cfg = build_client_from_configs(args.app_config, args.dingpan_config)
        keep = args.keep if args.keep is not None else cfg["keep"]
        report = run_backup(
            db_path=args.db,
            out_dir=args.out_dir,
            client=client,
            space_name=cfg["space_name"],
            keep=keep,
            dry_run=args.dry_run,
            union_id=cfg["union_id"],
            parent_id=cfg["parent_id"],
            viewer_user_id=cfg.get("viewer_user_id"),
            corp_id=cfg.get("corp_id"),
            log_path=cfg.get("log_path"),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
