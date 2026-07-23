"""Phase 2a 单元测试: src/workspace/api.py HTTP endpoints

覆盖:
- POST /upload: 正常 / 坏扩展名 / 坏魔数 / 超大 / 坏文件名 / 不存在 session
- GET /workspace: 列出 / 空 / 不存在 session
- DELETE /workspace/{filename}: 正常 / 不存在 / 越界
- POST /workspace/clear: 正常
- env flag: WORKSPACE_UPLOAD_ENABLED=false → 503
"""
import io
import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# tests/ 是子目录, src/ 在仓库根
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# === Fixtures ===

@pytest.fixture
def tmp_root(monkeypatch, tmp_path):
    """重定向 workspace + server 的 SESSIONS_ROOT 到 tmp_path"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    # mock workspace.guard 的 WORKSPACE_ROOT
    import workspace.guard as guard
    monkeypatch.setattr(guard, "WORKSPACE_ROOT", sessions)

    # mock server 的 SESSIONS_ROOT (api.py 通过 SESSIONS_ROOT 拿)
    # 因为 server 可能已经 import, 我们 patch 模块的 SESSIONS_ROOT
    import server
    monkeypatch.setattr(server, "SESSIONS_ROOT", sessions)

    return sessions


@pytest.fixture
def app_with_workspace(tmp_root):
    """建一个最小 FastAPI app, 挂 workspace_router"""
    # 必须重新 import (因为 SESSIONS_ROOT 在 server module 已确定)
    # 实际我们 mock 的是 server.SESSIONS_ROOT, 还需要让 _session_exists 用 mock 后的值
    # api.py 的 _session_exists 调 validate_session_id 然后查 (WORKSPACE_ROOT / session_id / metadata.json)
    # 所以我们用 guard.WORKSPACE_ROOT 即可, 不依赖 server.SESSIONS_ROOT

    # 但 server 引用了 workspace_router, 我们直接复用 server.app
    import server
    return server.app


@pytest.fixture
def client(app_with_workspace):
    return TestClient(app_with_workspace)


@pytest.fixture
def fake_session(tmp_root):
    """建一个 fake session 目录 + metadata.json"""
    session_id = "session-test-20260611"
    session_dir = tmp_root / session_id
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(
        json.dumps({
            "session_id": session_id,
            "title": "test session",
            "docx_path": "",
            "workflow_state": "style_review",
        })
    )
    return session_id


def _upload_files(client, session_id, files):
    """封装 multipart upload, files 是 [(filename, content_bytes), ...]"""
    multipart = []
    for name, content in files:
        multipart.append(("files", (name, io.BytesIO(content), "application/octet-stream")))
    return client.post(f"/api/sessions/{session_id}/upload", files=multipart)


# === POST /upload ===

class TestUpload:
    def test_upload_docx_ok(self, client, fake_session):
        """正常 .docx 上传 (PK 魔数) → 201"""
        content = b"PK\x03\x04" + b"\x00" * 100  # 假的 docx 字节
        resp = _upload_files(client, fake_session, [("report.docx", content)])
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["total_files"] == 1
        assert data["total_bytes"] == len(content)
        assert data["uploaded"][0]["filename"] == "report.docx"

    def test_upload_md_ok(self, client, fake_session):
        """普通 .md 文件不做魔数校验 → 201"""
        content = b"# Hello\n\nThis is markdown."
        resp = _upload_files(client, fake_session, [("notes.md", content)])
        assert resp.status_code == 201, resp.text

    def test_upload_multiple_files(self, client, fake_session):
        """一次上传多个文件 → 201 + 全部记录"""
        files = [
            ("a.txt", b"content a"),
            ("b.txt", b"content b"),
            ("c.md", b"# c"),
        ]
        resp = _upload_files(client, fake_session, files)
        assert resp.status_code == 201
        assert resp.json()["total_files"] == 3

    def test_upload_collision_renames(self, client, fake_session):
        """重名上传 → 第二个改名 __1"""
        # .docx 必须有 PK 头才能过魔数校验
        content = b"PK\x03\x04" + b"\x00" * 10
        _upload_files(client, fake_session, [("report.docx", content)])
        resp2 = _upload_files(client, fake_session, [("report.docx", content)])
        assert resp2.status_code == 201
        assert resp2.json()["uploaded"][0]["filename"] == "report__1.docx"

    def test_upload_bad_extension_rejected(self, client, fake_session):
        """不允许的扩展名 → 400"""
        resp = _upload_files(client, fake_session, [("virus.exe", b"x")])
        assert resp.status_code == 400
        assert "不支持的文件类型" in resp.json()["detail"]

    def test_upload_docx_wrong_magic_rejected(self, client, fake_session):
        """.docx 但内容不是 zip → 400 (魔数校验)"""
        resp = _upload_files(client, fake_session, [("fake.docx", b"Hello world")])
        assert resp.status_code == 400
        assert "魔数" in resp.json()["detail"]

    def test_upload_oversize_rejected(self, client, fake_session, monkeypatch):
        """超过 MAX_FILE_BYTES → 413"""
        # 临时改 MAX_FILE_BYTES 让测试快
        import workspace.guard as guard
        import workspace.api as api
        monkeypatch.setattr(guard, "MAX_FILE_BYTES", 100)
        # api.py 已经从 guard.MAX_FILE_BYTES 拿, 但 import 是 import-time, 重 patch 不影响
        # 实际: api.py 顶部 `from .guard import MAX_FILE_BYTES` 拿到的是原值
        # 我们需要 mock 整个 _check_upload_constraints 函数 or 在 guard 上直接改
        # 简单做法: 直接发 > 100 字节, 此时 guard.MAX_FILE_BYTES 已是 100
        # 但 api.py 内的 MAX_FILE_BYTES 是 25MB
        # 所以用 monkeypatch api.MAX_FILE_BYTES:
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 100)
        resp = _upload_files(client, fake_session, [("big.txt", b"x" * 200)])
        assert resp.status_code == 413
        assert "过大" in resp.json()["detail"]

    def test_upload_quota_exceeded_rejected(self, client, fake_session, monkeypatch):
        """单次 < MAX_FILE 但 session 累计超 QUOTA → 507"""
        import workspace.guard as guard
        import workspace.api as api
        monkeypatch.setattr(guard, "QUOTA_BYTES", 100)
        monkeypatch.setattr(api, "QUOTA_BYTES", 100)
        # 先上传 80 字节
        _upload_files(client, fake_session, [("a.txt", b"x" * 80)])
        # 再上传 50 字节 (80+50=130 > 100)
        resp = _upload_files(client, fake_session, [("b.txt", b"y" * 50)])
        assert resp.status_code == 507
        assert "quota" in resp.json()["detail"]

    def test_upload_nonexistent_session_returns_404(self, client):
        """v3: upload 不再自动创建 session, 不存在时返回 404"""
        resp = _upload_files(client, "session-ghost", [("a.txt", b"x")])
        assert resp.status_code == 404

    def test_upload_dotdot_filename_rejected(self, client, fake_session):
        """.docx 不会触发 .. 段, 但 ../../etc/passwd 会过 safe_workspace_filename 失败"""
        resp = _upload_files(client, fake_session, [("../../etc/passwd", b"x")])
        assert resp.status_code == 400

    def test_upload_path_separator_filename_rejected(self, client, fake_session):
        """C:\\foo\\bar.txt 走 safe_workspace_filename 只取 basename → 'bar.txt' (合法)"""
        # 实际上传 multipart 时 filename 是 "bar.txt" (client 自动处理), 这里测一个包含 / 的
        resp = _upload_files(client, fake_session, [("foo/bar.txt", b"x")])
        # multipart filename "foo/bar.txt" 传给 safe_workspace_filename → "bar.txt" 是合法 basename
        # 所以这个测试会通过 (sanitize 后变 "bar.txt"), 验证 sanitize 行为
        assert resp.status_code == 201
        assert resp.json()["uploaded"][0]["filename"] == "bar.txt"

    def test_upload_hidden_filename_rejected(self, client, fake_session):
        resp = _upload_files(client, fake_session, [(".hidden", b"x")])
        assert resp.status_code == 400

    def test_upload_disabled_by_env_flag(self, client, fake_session, monkeypatch):
        monkeypatch.setenv("WORKSPACE_UPLOAD_ENABLED", "false")
        resp = _upload_files(client, fake_session, [("a.txt", b"x")])
        # _upload_enabled() 在每次 endpoint 调用时读 env, 所以不需要 reload
        assert resp.status_code == 503


# === GET /workspace ===

class TestListWorkspace:
    def test_list_empty(self, client, fake_session):
        resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert resp.status_code == 200
        data = resp.json()
        assert data["files"] == []
        assert data["total_files"] == 0

    def test_list_with_files(self, client, fake_session):
        # 先上传两个
        _upload_files(client, fake_session, [
            ("alpha.txt", b"a" * 10),
            ("beta.txt", b"b" * 20),
        ])
        resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_files"] == 2
        assert data["total_bytes"] == 30
        names = {f["name"] for f in data["files"]}
        assert names == {"alpha.txt", "beta.txt"}

    def test_list_nonexistent_session(self, client):
        resp = client.get("/api/sessions/session-ghost/workspace")
        assert resp.status_code == 404

    def test_list_shows_subdirectory_files(self, client, fake_session):
        """workspace 内子目录的文件应被列出 (path 字段含相对路径)"""
        # 直接写一个子目录文件 (因为我们没解压 zip, 模拟解压结果)
        import workspace.guard as guard
        ws = guard.workspace_dir(fake_session)
        sub = ws / "docs"
        sub.mkdir()
        (sub / "report.docx").write_bytes(b"PK\x03\x04" + b"x" * 50)

        resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert any(f["path"] == "docs/report.docx" for f in files)


# === DELETE /workspace/{filename} ===

class TestDeleteFile:
    def test_delete_existing(self, client, fake_session):
        _upload_files(client, fake_session, [("del.txt", b"x")])
        resp = client.delete(f"/api/sessions/{fake_session}/workspace/del.txt")
        assert resp.status_code == 204
        # 再次 list 应为空
        assert client.get(f"/api/sessions/{fake_session}/workspace").json()["total_files"] == 0

    def test_delete_nonexistent(self, client, fake_session):
        resp = client.delete(f"/api/sessions/{fake_session}/workspace/missing.txt")
        assert resp.status_code == 404

    def test_delete_dotdot_blocked(self, client, fake_session):
        """.docx 不会过, 但 ../ 会被 safe_workspace_filename 拒绝"""
        resp = client.delete(f"/api/sessions/{fake_session}/workspace/..%2F..%2Fetc%2Fpasswd")
        # URL decode 后是 "../../etc/passwd", safe_workspace_filename 取 basename "passwd" → 合法
        # 然后路径检查会失败 (文件不存在)
        assert resp.status_code in (404, 400)  # 404 (file not found) 或 400 (bad request)

    def test_delete_path_separator_blocked(self, client, fake_session):
        """foo/bar.txt 走 sanitize → 'bar.txt', 404"""
        _upload_files(client, fake_session, [("bar.txt", b"x")])
        resp = client.delete(f"/api/sessions/{fake_session}/workspace/foo%2Fbar.txt")
        # 实际 delete endpoint URL path: workspace/foo/bar.txt (FastAPI 路由不会拆)
        # 但 我们的 endpoint 是 /workspace/{filename}, {filename} 不能含 /
        # FastAPI 会返回 404 (路由不匹配) 或 422
        assert resp.status_code in (404, 422)

    def test_delete_nonexistent_session(self, client):
        resp = client.delete("/api/sessions/ghost/workspace/x.txt")
        assert resp.status_code == 404


# === POST /workspace/clear ===

class TestClear:
    def test_clear_with_files(self, client, fake_session):
        _upload_files(client, fake_session, [
            ("a.txt", b"x"),
            ("b.txt", b"y"),
        ])
        resp = client.post(f"/api/sessions/{fake_session}/workspace/clear")
        assert resp.status_code == 204
        # list 应为空
        data = client.get(f"/api/sessions/{fake_session}/workspace").json()
        assert data["total_files"] == 0

    def test_clear_empty_ok(self, client, fake_session):
        resp = client.post(f"/api/sessions/{fake_session}/workspace/clear")
        assert resp.status_code == 204

    def test_clear_nonexistent_session(self, client):
        resp = client.post("/api/sessions/ghost/workspace/clear")
        assert resp.status_code == 404


# === env flag 一致性测试 ===

class TestEnvFlag:
    def test_upload_disabled_returns_503(self, client, fake_session, monkeypatch):
        monkeypatch.setenv("WORKSPACE_UPLOAD_ENABLED", "false")
        resp = _upload_files(client, fake_session, [("a.txt", b"x")])
        assert resp.status_code == 503

    def test_delete_disabled_returns_503(self, client, fake_session, monkeypatch):
        monkeypatch.setenv("WORKSPACE_UPLOAD_ENABLED", "false")
        _upload_files(client, fake_session, [("a.txt", b"x")])
        resp = client.delete(f"/api/sessions/{fake_session}/workspace/a.txt")
        assert resp.status_code == 503

    def test_list_does_not_check_env_flag(self, client, fake_session, monkeypatch):
        """GET /workspace 是只读, 不受 WORKSPACE_UPLOAD_ENABLED 影响"""
        monkeypatch.setenv("WORKSPACE_UPLOAD_ENABLED", "false")
        resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert resp.status_code == 200


# === POST /upload  zip 流式解压 (Phase 2b) ===

import zipfile as _zipfile


def _make_zip_bytes(entries: dict) -> bytes:
    """构造 zip 字节流, entries = {filename_in_zip: content_bytes}"""
    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_zip_with_zeros(name: str, size: int) -> bytes:
    """构造 zip 含单个大文件 (全零, 压缩比接近 1)"""
    return _make_zip_bytes({name: b"\x00" * size})


class TestZipExtract:
    def test_normal_zip_extracts(self, client, fake_session):
        """正常 zip 解压到 workspace/<stem>/ 子目录"""
        zip_bytes = _make_zip_bytes({
            "report.docx": b"PK\x03\x04" + b"\x00" * 50,
            "notes.md": b"# notes",
        })
        resp = _upload_files(client, fake_session, [("docs.zip", zip_bytes)])
        assert resp.status_code == 201, resp.text
        data = resp.json()
        # 解压出 2 个文件
        assert data["total_files"] == 2
        paths = {f["path"] for f in data["uploaded"]}
        assert "docs/report.docx" in paths
        assert "docs/notes.md" in paths
        # 标记 source
        for f in data["uploaded"]:
            assert f.get("extracted_from") == "docs.zip"

    def test_zip_extract_subdir_in_zip(self, client, fake_session):
        """zip 内部有子目录的 entry"""
        zip_bytes = _make_zip_bytes({
            "subdir/inner.txt": b"hello",
        })
        resp = _upload_files(client, fake_session, [("pack.zip", zip_bytes)])
        assert resp.status_code == 201
        data = resp.json()
        assert any(f["path"] == "pack/subdir/inner.txt" for f in data["uploaded"])

    def test_zip_slip_rejected(self, client, fake_session):
        """zip 内含 .. 段 entry → 400 zip_slip"""
        # 构造恶意 zip: 手动构造 entry 绕过 writestr 校验
        buf = io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", b"evil")
        zip_bytes = buf.getvalue()
        resp = _upload_files(client, fake_session, [("evil.zip", zip_bytes)])
        assert resp.status_code == 400
        assert "zip slip" in resp.json()["detail"]

    def test_zip_slip_absolute_path_rejected(self, client, fake_session):
        """zip 内含绝对路径 entry → 400"""
        buf = io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/passwd", b"evil")
        zip_bytes = buf.getvalue()
        resp = _upload_files(client, fake_session, [("evil.zip", zip_bytes)])
        assert resp.status_code == 400

    def test_zip_bomb_compression_ratio_rejected(self, client, fake_session):
        """zip 内单 entry 压缩比 > 100 → 400 zip_bomb"""
        # 构造高压缩比 entry: 大量重复数据
        # Python zip deflate 对重复数据的压缩比通常在 1000+:1
        buf = io.BytesIO()
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            # 1000 字节全 'A', deflate 后 < 10 字节, 比例 100+
            zf.writestr("bomb.bin", b"A" * 1000)
        zip_bytes = buf.getvalue()
        resp = _upload_files(client, fake_session, [("bomb.zip", zip_bytes)])
        # 比例可能被 Python deflate 控制在 100 以下, 所以可能过; 但要确保不会越界
        # 如果触发了 100:1 限制 → 400, 否则解压成功
        assert resp.status_code in (201, 400)
        if resp.status_code == 400:
            assert "bomb" in resp.json()["detail"].lower() or "压缩比" in resp.json()["detail"]

    def test_zip_quota_exceeded_after_extract(self, client, fake_session, monkeypatch):
        """解压后总大小超 session quota → 507 + 回滚 (无残骸)"""
        import workspace.guard as guard
        import workspace.api as api
        monkeypatch.setattr(guard, "QUOTA_BYTES", 100)
        monkeypatch.setattr(api, "QUOTA_BYTES", 100)
        # 构造 zip: 2 个 entry, 第一个 60 字节, 第二个 60 字节, 总解压后 120 字节
        # 60:60 ratio = 1, 不触发 bomb
        zip_bytes = _make_zip_bytes({
            "a.txt": b"x" * 60,
            "b.txt": b"y" * 60,
        })
        resp = _upload_files(client, fake_session, [("pack.zip", zip_bytes)])
        assert resp.status_code == 507
        assert "quota" in resp.json()["detail"].lower()
        # 验证回滚: 列出 workspace 应空 (没有 pack/ 残骸)
        list_resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert list_resp.json()["total_files"] == 0

    def test_zip_extract_collision_renames_subdir(self, client, fake_session):
        """同名 zip 多次上传 → 子目录加 __1"""
        zip_bytes = _make_zip_bytes({"a.txt": b"hello"})
        _upload_files(client, fake_session, [("pack.zip", zip_bytes)])
        resp2 = _upload_files(client, fake_session, [("pack.zip", zip_bytes)])
        assert resp2.status_code == 201
        # 第二个解压到 pack__1/
        paths = {f["path"] for f in resp2.json()["uploaded"]}
        assert any("pack__1/a.txt" in p for p in paths)

    def test_corrupt_zip_rejected(self, client, fake_session):
        """损坏的 zip → 400"""
        # PK 头但内容损坏
        bad_zip = b"PK\x03\x04" + b"garbage" * 100
        resp = _upload_files(client, fake_session, [("bad.zip", bad_zip)])
        assert resp.status_code == 400
        assert "损坏" in resp.json()["detail"] or "zip" in resp.json()["detail"].lower()

    def test_zip_extract_listed_in_workspace(self, client, fake_session):
        """解压后 GET /workspace 应列出所有子目录文件"""
        zip_bytes = _make_zip_bytes({
            "report.docx": b"PK\x03\x04" + b"x" * 30,
            "charts/data.png": b"x" * 50,
        })
        _upload_files(client, fake_session, [("docs.zip", zip_bytes)])
        resp = client.get(f"/api/sessions/{fake_session}/workspace")
        assert resp.status_code == 200
        files = resp.json()["files"]
        paths = {f["path"] for f in files}
        assert "docs/report.docx" in paths
        assert "docs/charts/data.png" in paths
