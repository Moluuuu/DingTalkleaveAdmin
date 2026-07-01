"""Tests for the real DingTalk Drive client wrapper.

Network operations are mocked. These tests verify request shape and guardrails,
not DingTalk availability.
"""
import json

from leaveadmin.dingpan_backup import DingTalkDriveClient, BackupError


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = []

    def add(self, response):
        self.responses.append(response)

    def _pop(self):
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._pop()

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._pop()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._pop()

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return self._pop()


def test_query_dentry_id_by_uuid_uses_doc_mapping_api():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={"dentryUuid": "uuid1", "dentryId": "12345", "spaceId": "space"}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    result = client.query_dentry_id_by_uuid("uuid1")

    assert result["dentryId"] == "12345"
    method, url, kwargs = session.calls[1]
    assert method == "GET"
    assert url.endswith("/v2.0/doc/dentries/uuid1/queryDentryId")
    assert kwargs["params"] == {"operatorId": "union"}


def test_get_download_info_uses_storage_download_info_api():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={
        "protocol": "HEADER_SIGNATURE",
        "headerSignatureInfo": {
            "resourceUrls": ["https://download.example/file"],
            "headers": {"Authorization": "sig"},
        },
    }))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    info = client.get_download_info("space", "dentry1")

    assert info["headerSignatureInfo"]["resourceUrls"] == ["https://download.example/file"]
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/v1.0/storage/spaces/space/dentries/dentry1/downloadInfos/query")
    assert kwargs["params"] == {"unionId": "union"}
    assert kwargs["json"] == {"option": {"version": 1, "preferIntranet": False}}


def test_download_file_writes_signed_resource_to_destination(tmp_path):
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={
        "protocol": "HEADER_SIGNATURE",
        "headerSignatureInfo": {
            "resourceUrls": ["https://download.example/file"],
            "headers": {"Authorization": "sig"},
        },
    }))
    session.add(FakeResponse(data={}, text="backup-bytes"))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)
    dest = tmp_path / "backup.tar.gz"

    client.download_file("space", "dentry1", dest)

    assert dest.read_bytes() == b"backup-bytes"
    method, url, kwargs = session.calls[2]
    assert method == "GET"
    assert url == "https://download.example/file"
    assert kwargs["headers"] == {"Authorization": "sig"}


def test_download_latest_backup_picks_newest_by_filename_when_search_time_is_zero(tmp_path):
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={"items": [
        {"dentryUuid": "old", "name": "leaveAdmin-backup-20260628-010000.tar.gz", "modified_time": 0},
        {"dentryUuid": "new", "name": "leaveAdmin-backup-20260628-020000.tar.gz", "modified_time": 0},
    ]}))
    session.add(FakeResponse(data={"dentryUuid": "new", "dentryId": "12345", "spaceId": "space"}))
    session.add(FakeResponse(data={
        "protocol": "HEADER_SIGNATURE",
        "headerSignatureInfo": {
            "resourceUrls": ["https://download.example/new"],
            "headers": {},
        },
    }))
    session.add(FakeResponse(data={}, text="new-backup"))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    result = client.download_latest_backup("space", tmp_path)

    assert result["name"] == "leaveAdmin-backup-20260628-020000.tar.gz"
    assert result["file_id"] == "12345"
    assert result["path"].read_bytes() == b"new-backup"
    method, url, kwargs = session.calls[2]
    assert method == "GET"
    assert url.endswith("/v2.0/doc/dentries/new/queryDentryId")
    method, url, kwargs = session.calls[3]
    assert method == "POST"
    assert url.endswith("/v1.0/storage/spaces/space/dentries/12345/downloadInfos/query")


def test_get_access_token_uses_app_key_secret():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    assert client.get_access_token() == "token123"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/v1.0/oauth2/accessToken")
    assert kwargs["json"] == {"appKey": "ak", "appSecret": "sk"}


def test_existing_space_id_does_not_require_root_dentry_uuid():
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", space_id="space", session=FakeSession())

    assert client.ensure_space("公休余额灾备") == "space"


def test_missing_space_id_creates_visible_drive_space():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={"spaceId": "new_space", "spaceName": "公休余额灾备"}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    assert client.ensure_space("公休余额灾备") == "new_space"
    assert client.last_created_space["spaceName"] == "公休余额灾备"
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/v1.0/drive/spaces")
    assert kwargs["json"] == {"name": "公休余额灾备", "unionId": "union"}


def test_get_upload_info_and_put_upload_shape(tmp_path):
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={
        "headerSignatureUploadInfo": {
            "mediaId": "media_1",
            "resourceUrls": ["https://upload.example/file"],
            "headers": {"Authorization": "sig"},
        },
    }))
    session.add(FakeResponse(data={}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"abc")

    info = client.get_upload_info("space", "0", "a.tar.gz", 3, "md5")
    assert info["media_id"] == "media_1"
    client.oss_upload(info, local)

    upload_call = session.calls[1]
    assert upload_call[0] == "POST"
    assert "/v1.0/storage/spaces/space/files/uploadInfos/query" in upload_call[1]
    assert upload_call[2]["params"] == {"unionId": "union"}
    assert upload_call[2]["json"] == {
        "protocol": "HEADER_SIGNATURE",
        "multipart": False,
        "option": {
            "storageDriver": "DINGTALK",
            "preCheckParam": {
                "md5": "md5",
                "size": 3,
                "parentId": "0",
                "name": "a.tar.gz",
            },
            "preferRegion": "ZHANGJIAKOU",
            "preferIntranet": False,
        },
    }

    put_call = session.calls[2]
    assert put_call[0] == "PUT"
    assert put_call[1] == "https://upload.example/file"
    assert put_call[2]["headers"] == {"Authorization": "sig", "Content-Type": ""}


def test_get_upload_info_fails_loudly_without_media_id():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={"stsUploadInfo": {}}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    try:
        client.get_upload_info("space", "0", "a.tar.gz", 3, "md5")
    except BackupError as exc:
        assert "mediaId" in str(exc)
    else:
        raise AssertionError("expected BackupError")


def test_add_file_uses_storage_commit_api_and_returns_file_id():
    session = FakeSession()
    session.add(FakeResponse(data={"accessToken": "token123"}))
    session.add(FakeResponse(data={"dentry": {"id": "file1", "spaceId": "space", "name": "b.tar.gz"}}))
    client = DingTalkDriveClient(app_key="ak", app_secret="sk", union_id="union", session=session)

    result = client.add_file("space", "0", "b.tar.gz", "upload_key_1")
    assert result["file_id"] == "file1"
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/v1.0/storage/spaces/space/files/commit")
    assert kwargs["params"] == {"unionId": "union"}
    assert kwargs["json"] == {
        "uploadKey": "upload_key_1",
        "name": "b.tar.gz",
        "parentId": "0",
        "option": {
            "conflictStrategy": "AUTO_RENAME",
            "convertToOnlineDoc": False,
        },
    }
