from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import alibabacloud_oss_v2 as oss
import qrcode
import requests

from src.config import Settings


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class AuthSession:
    uid: str
    sign: str
    check_time: int
    verifier: str
    qr_path: Path


@dataclass(slots=True)
class OfflineTaskInfo:
    name: str
    url: str
    info_hash: str
    file_id: str
    status: int
    percent_done: int


@dataclass(slots=True)
class RemoteFile:
    name: str
    pick_code: str
    relative_path: str


class Open115Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = "https://proapi.115.com"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.access_token = settings.access_token
        self.refresh_token = settings.refresh_token
        self._load_tokens_from_disk()

    def _load_tokens_from_disk(self) -> None:
        if not self.settings.token_file.exists():
            return
        with self.settings.token_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.access_token = data.get("access_token", self.access_token)
        self.refresh_token = data.get("refresh_token", self.refresh_token)

    def _save_tokens(self) -> None:
        self.settings.token_file.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.token_file.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                },
                handle,
            )

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise RuntimeError("115 access_token is missing")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        require_auth: bool = True,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        req_headers = headers or (self._auth_headers() if require_auth else {"User-Agent": USER_AGENT})
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            data=data,
            headers=req_headers,
            timeout=(10, 60),
        )
        response.raise_for_status()
        payload = response.json()
        if require_auth and allow_refresh and payload.get("code") == 40140125:
            self.refresh_access_token()
            return self._request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                require_auth=require_auth,
                allow_refresh=False,
            )
        return payload

    def refresh_access_token(self) -> None:
        self._load_tokens_from_disk()
        if not self.refresh_token:
            raise RuntimeError("115 refresh_token is missing")
        payload = self._request(
            "POST",
            "https://passportapi.115.com/open/refreshToken",
            data={"refresh_token": self.refresh_token},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            require_auth=False,
        )
        data = payload.get("data") or {}
        if not payload.get("state") or not data.get("access_token"):
            raise RuntimeError(f"115 refresh token failed: {payload}")
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self._save_tokens()

    def create_auth_session(self, qr_path: str | Path) -> AuthSession:
        if not self.settings.app_id_115:
            raise RuntimeError("115_app_id is required for /auth")
        verifier, challenge = self._create_pkce_pair()
        payload = self._request(
            "POST",
            "https://passportapi.115.com/open/authDeviceCode",
            data={
                "client_id": self.settings.app_id_115,
                "code_challenge": challenge,
                "code_challenge_method": "sha256",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            require_auth=False,
        )
        data = payload.get("data") or {}
        if not payload.get("data"):
            raise RuntimeError(f"115 auth session creation failed: {payload}")

        qr_file = Path(qr_path)
        qr_file.parent.mkdir(parents=True, exist_ok=True)
        image = qrcode.make(data["qrcode"])
        image.save(qr_file)
        return AuthSession(
            uid=data["uid"],
            sign=data["sign"],
            check_time=int(data["time"]),
            verifier=verifier,
            qr_path=qr_file,
        )

    def wait_for_auth(self, auth_session: AuthSession, timeout: int = 300, interval: int = 2) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self._request(
                "GET",
                "https://qrcodeapi.115.com/get/status/",
                params={
                    "uid": auth_session.uid,
                    "time": auth_session.check_time,
                    "sign": auth_session.sign,
                },
                require_auth=False,
            )
            if payload.get("state") == 0:
                raise RuntimeError("115 auth QR code expired")

            status = (payload.get("data") or {}).get("status")
            if status == 2:
                token_payload = self._request(
                    "POST",
                    "https://passportapi.115.com/open/deviceCodeToToken",
                    data={
                        "uid": auth_session.uid,
                        "code_verifier": auth_session.verifier,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": USER_AGENT,
                    },
                    require_auth=False,
                )
                token_data = token_payload.get("data") or {}
                if not token_data.get("access_token"):
                    raise RuntimeError(f"115 auth failed: {token_payload}")
                self.access_token = token_data["access_token"]
                self.refresh_token = token_data["refresh_token"]
                self._save_tokens()
                return
            time.sleep(interval)
        raise TimeoutError("115 auth timed out")

    def get_user_info(self) -> dict[str, Any]:
        payload = self._request("GET", f"{self.base_url}/open/user/info")
        data = payload.get("data")
        if payload.get("code") != 0 or not data:
            raise RuntimeError(f"115 get_user_info failed: {payload}")
        return data

    def get_file_info(self, path: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            f"{self.base_url}/open/folder/get_info",
            params={"path": os.path.normpath(path)},
        )
        if payload.get("code") == 0:
            return payload.get("data")
        return None

    def get_file_info_by_id(self, file_id: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            f"{self.base_url}/open/folder/get_info",
            params={"file_id": file_id},
        )
        if payload.get("code") == 0:
            return payload.get("data")
        return None

    def create_directory(self, pid: int, name: str) -> dict[str, Any] | None:
        payload = self._request(
            "POST",
            f"{self.base_url}/open/folder/add",
            data={"pid": pid, "file_name": name},
        )
        if payload.get("state") is True or payload.get("code") == 0:
            return payload.get("data") or {}
        if payload.get("code") == 20004:
            return {}
        raise RuntimeError(f"115 create directory failed: {payload}")

    def create_dir_recursive(self, path: str) -> dict[str, Any]:
        normalized = os.path.normpath(path)
        current = ""
        info: dict[str, Any] | None = None
        for segment in _parent_paths(normalized):
            info = self.get_file_info(segment)
            if info:
                current = segment
                continue
            parent_id = 0
            if current:
                parent_info = self.get_file_info(current)
                if not parent_info:
                    raise RuntimeError(f"115 parent directory not found: {current}")
                parent_id = int(parent_info["file_id"])
            self.create_directory(parent_id, os.path.basename(segment))
            info = self.get_file_info(segment)
            if not info:
                raise RuntimeError(f"115 failed to create directory: {segment}")
            current = segment
        if not info:
            raise RuntimeError(f"115 target directory unavailable: {path}")
        return info

    def add_offline_task(self, magnet: str, save_path: str) -> None:
        folder_info = self.get_file_info(save_path) or self.create_dir_recursive(save_path)
        payload = self._request(
            "POST",
            f"{self.base_url}/open/offline/add_task_urls",
            data={"urls": magnet, "wp_path_id": folder_info["file_id"]},
        )
        if payload.get("state") is not True:
            raise RuntimeError(f"115 add offline task failed: {payload}")

    def list_offline_tasks(self, max_pages: int | None = None) -> list[OfflineTaskInfo]:
        first_page = self._request("GET", f"{self.base_url}/open/offline/get_task_list")
        if first_page.get("code") != 0:
            raise RuntimeError(f"115 get offline tasks failed: {first_page}")

        tasks: list[OfflineTaskInfo] = []
        total_pages = int((first_page.get("data") or {}).get("page_count", 1))
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)

        first_page_data = first_page.get("data") or {}
        for task in first_page_data.get("tasks", []):
            tasks.append(
                OfflineTaskInfo(
                    name=task.get("name", ""),
                    url=task.get("url", ""),
                    info_hash=task.get("info_hash", ""),
                    file_id=str(task.get("file_id", "")),
                    status=int(task.get("status", 0)),
                    percent_done=int(task.get("percentDone", 0)),
                )
            )

        for page in range(2, total_pages + 1):
            try:
                payload = self._request(
                    "GET",
                    f"{self.base_url}/open/offline/get_task_list",
                    params={"page": page},
                )
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 405:
                    break
                raise
            data = payload.get("data") or {}
            for task in data.get("tasks", []):
                tasks.append(
                    OfflineTaskInfo(
                        name=task.get("name", ""),
                        url=task.get("url", ""),
                        info_hash=task.get("info_hash", ""),
                        file_id=str(task.get("file_id", "")),
                        status=int(task.get("status", 0)),
                        percent_done=int(task.get("percentDone", 0)),
                    )
                )
        return tasks

    def wait_offline_complete(self, magnet: str, timeout: int, interval: int) -> OfflineTaskInfo:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for task in self.list_offline_tasks(max_pages=3):
                if task.url != magnet:
                    continue
                if task.status == 2 or task.percent_done >= 100:
                    return task
            time.sleep(interval)
        raise TimeoutError("115 offline task timed out")

    def get_file_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._request("GET", f"{self.base_url}/open/ufile/files", params=params)
        if payload.get("code") != 0:
            raise RuntimeError(f"115 get file list failed: {payload}")
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("list"), list):
                return data["list"]
            if isinstance(data.get("data"), list):
                return data["data"]
        return []

    def list_downloadable_files(self, root_path: str) -> list[RemoteFile]:
        info = self.get_file_info(root_path)
        if not info:
            raise RuntimeError(f"115 file path not found: {root_path}")
        if str(info.get("file_category")) != "0":
            file_name = str(info.get("file_name") or Path(root_path).name)
            pick_code = str(info.get("pick_code") or info.get("pc") or "")
            if not pick_code:
                raise RuntimeError(f"115 pick_code missing for file: {root_path}")
            return [RemoteFile(name=file_name, pick_code=pick_code, relative_path=file_name)]
        return self._walk_files(str(info["file_id"]), "")

    def _walk_files(self, cid: str, prefix: str) -> list[RemoteFile]:
        entries = self.get_file_list({"cid": cid, "limit": 1000, "show_dir": 1})
        files: list[RemoteFile] = []
        for entry in entries:
            name = str(entry.get("fn") or entry.get("file_name") or entry.get("n") or "")
            if not name:
                continue
            if str(entry.get("fc")) == "0":
                next_cid = str(entry.get("fid") or entry.get("cid") or "")
                child_prefix = f"{prefix}/{name}" if prefix else name
                files.extend(self._walk_files(next_cid, child_prefix))
                continue
            pick_code = str(entry.get("pc") or entry.get("pick_code") or "")
            relative_path = f"{prefix}/{name}" if prefix else name
            files.append(RemoteFile(name=name, pick_code=pick_code, relative_path=relative_path))
        return files

    def get_download_url(self, pick_code: str) -> str:
        payload = self._request(
            "POST",
            f"{self.base_url}/open/ufile/downurl",
            data={"pick_code": pick_code},
        )
        if payload.get("state") is not True:
            raise RuntimeError(f"115 get download url failed: {payload}")
        data = payload.get("data") or {}
        first_value = next(iter(data.values()), None)
        if not first_value:
            raise RuntimeError("115 download url response is empty")
        return first_value["url"]["url"]

    def upload_file(self, file_path: str | Path, target_path: str) -> tuple[bool, bool]:
        local_path = Path(file_path)
        if not local_path.exists():
            raise FileNotFoundError(local_path)

        folder_info = self.get_file_info(target_path) or self.create_dir_recursive(target_path)
        sha1_value = _file_sha1(local_path)
        return self._upload_file_impl(
            local_path=local_path,
            target_folder_id=str(folder_info["file_id"]),
            file_sha1=sha1_value,
            request_times=1,
        )

    def _upload_file_impl(
        self,
        *,
        local_path: Path,
        target_folder_id: str,
        file_sha1: str,
        request_times: int,
        sign_key: str | None = None,
        sign_val: str | None = None,
    ) -> tuple[bool, bool]:
        payload = self._request(
            "POST",
            f"{self.base_url}/open/upload/init",
            data={
                "file_name": local_path.name,
                "file_size": local_path.stat().st_size,
                "target": f"U_1_{target_folder_id}",
                "fileid": file_sha1,
                **({"sign_key": sign_key, "sign_val": sign_val} if sign_key and sign_val else {}),
            },
        )
        if payload.get("code") != 0:
            raise RuntimeError(f"115 upload init failed: {payload}")

        data = payload.get("data") or {}
        if data.get("sign_key") and data.get("sign_check") and request_times == 1:
            start, end = [int(part) for part in str(data["sign_check"]).split("-")]
            return self._upload_file_impl(
                local_path=local_path,
                target_folder_id=target_folder_id,
                file_sha1=file_sha1,
                request_times=2,
                sign_key=str(data["sign_key"]),
                sign_val=_file_sha1_by_range(local_path, start, end).upper(),
            )

        if data.get("status") == 2:
            return True, True

        token_info = self.get_upload_token()
        callback = data.get("callback") or {}
        self._upload_via_oss(
            local_path=local_path,
            access_key_id=token_info["AccessKeyId"],
            access_key_secret=token_info["AccessKeySecret"],
            security_token=token_info["SecurityToken"],
            endpoint=token_info["endpoint"],
            bucket=data["bucket"],
            object_key=data["object"],
            callback=base64.b64encode(str(callback.get("callback", "{}")).encode()).decode(),
            callback_var=base64.b64encode(str(callback.get("callback_var", "{}")).encode()).decode(),
        )
        return True, False

    def get_upload_token(self) -> dict[str, Any]:
        payload = self._request("GET", f"{self.base_url}/open/upload/get_token")
        if payload.get("code") != 0:
            raise RuntimeError(f"115 get upload token failed: {payload}")
        return payload["data"]

    def _upload_via_oss(
        self,
        *,
        local_path: Path,
        access_key_id: str,
        access_key_secret: str,
        security_token: str,
        endpoint: str,
        bucket: str,
        object_key: str,
        callback: str,
        callback_var: str,
    ) -> None:
        credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            security_token=security_token,
        )
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials_provider
        cfg.region = "cn-shenzhen"
        cfg.endpoint = endpoint
        client = oss.Client(cfg)
        result = client.put_object_from_file(
            oss.PutObjectRequest(
                bucket=bucket,
                key=object_key,
                callback=callback,
                callback_var=callback_var,
            ),
            str(local_path),
        )
        if result.status_code != 200:
            raise RuntimeError(f"115 OSS upload failed: {result.status_code}")

    @staticmethod
    def _create_pkce_pair() -> tuple[str, str]:
        verifier_bytes = os.urandom(64)
        verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("utf-8")
        verifier = re.sub(r"[^A-Za-z0-9\-._~]", "", verifier)[:64]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        return verifier, challenge.rstrip(b"=").decode("utf-8")


def _parent_paths(path: str) -> list[str]:
    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)
    if parts[0] == "":
        parts[0] = os.sep
    current = parts[0] if parts[0] == os.sep else ""
    result: list[str] = []
    for part in parts[1:]:
        current = os.path.join(current, part)
        result.append(current)
    return result


def _file_sha1(path: Path) -> str:
    sha1 = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha1.update(chunk)
    return sha1.hexdigest()


def _file_sha1_by_range(path: Path, start: int, end: int) -> str:
    size = end - start + 1
    sha1 = hashlib.sha1()
    with path.open("rb") as handle:
        handle.seek(start)
        sha1.update(handle.read(size))
    return sha1.hexdigest()
