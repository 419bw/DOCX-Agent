"""v2: Workspace HTTP endpoints — 上传 / 列表 / 删除 / 清空

挂载到 src/server.py: app.include_router(workspace_router, prefix="/api/sessions")

设计:
- 上传: multipart/form-data, file 字段可重复;支持 .docx/.png/.md/.zip 等白名单
- 列表: GET /{session_id}/workspace, 排序 (mtime, name) 稳定 tuple
- 删除: DELETE /{session_id}/workspace/{filename}
- 清空: POST /{session_id}/workspace/clear

env flag: WORKSPACE_UPLOAD_ENABLED=false 时所有写端点返回 503
"""

import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import guard
from .errors import BadUpload
from .guard import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_BYTES,
    QUOTA_BYTES,
    safe_workspace_filename,
    unique_workspace_target,
    workspace_dir,
)

router = APIRouter(tags=["workspace"])

# === Magic bytes 校验白名单 (仅压缩格式强制) ===
_MAGIC_BYTES_EXTENSIONS = {".docx", ".zip", ".xlsx", ".pptx"}
_MAGIC_BYTES_PK = b"PK\x03\x04"
# 读前 N 字节做魔数检查就够
_MAGIC_CHECK_BYTES = 4

# === env flag: 上传功能总开关 ===
def _upload_enabled() -> bool:
    return os.environ.get("WORKSPACE_UPLOAD_ENABLED", "true").lower() != "false"


def _session_exists(session_id: str) -> bool:
    """session 是否存在 (有 metadata.json 即视为存在)

    注意: 不复制 guard.WORKSPACE_ROOT 到本地变量, 而是通过 guard.WORKSPACE_ROOT
    每次访问 — 这样测试可以 patch guard.WORKSPACE_ROOT 实时生效
    """
    from .guard import validate_session_id
    try:
        validate_session_id(session_id)
    except Exception:
        return False
    return (guard.WORKSPACE_ROOT / session_id / "metadata.json").exists()


def _workspace_size_bytes(workspace: Path) -> int:
    """累加 workspace 内所有文件大小 (递归)"""
    total = 0
    for entry in workspace.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _check_upload_constraints(
    extension: str,
    file_size: int,
    workspace: Path,
) -> None:
    """上传前约束检查: 扩展名 / 单文件大小 / session quota

    Raises:
        HTTPException(400/413/507)
    """
    if extension.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {extension!r}. 支持: {sorted(ALLOWED_EXTENSIONS)}",
        )
    if file_size > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大: {file_size} B > {MAX_FILE_BYTES} B ({MAX_FILE_BYTES // 1024 // 1024} MB)",
        )
    current = _workspace_size_bytes(workspace)
    if current + file_size > QUOTA_BYTES:
        raise HTTPException(
            status_code=507,
            detail=f"session quota 不足: 当前 {current} B + 上传 {file_size} B > {QUOTA_BYTES} B ({QUOTA_BYTES // 1024 // 1024} MB)",
        )


def _check_magic_bytes(extension: str, head_bytes: bytes) -> None:
    """仅对压缩格式做魔数校验, 普通文件 (.md/.txt/.png) 跳过

    v1 设计: 只校验 zip 魔数, 图片不强制 PNG/JPEG 头 (避免误伤 v1)
    """
    if extension.lower() in _MAGIC_BYTES_EXTENSIONS:
        if not head_bytes.startswith(_MAGIC_BYTES_PK):
            raise HTTPException(
                status_code=400,
                detail=f"{extension} 文件不是有效的 zip 容器 (魔数 {head_bytes[:4]!r} != PK\\x03\\x04)",
            )


# === zip 流式解压 (Phase 2b) ===

# 单 entry 压缩比上限: 防止 42.zip 风格炸弹 (典型比例 10000+:1)
_ZIP_ENTRY_MAX_RATIO = 100
# zip 内允许的 entry 数上限 (防 DOS via 大量小文件)
_ZIP_MAX_ENTRIES = 10000


def _extract_zip_streaming(
    zip_path: Path,
    target_dir: Path,
    workspace: Path,
) -> List[dict]:
    """流式解压 zip 到 target_dir

    Args:
        zip_path: zip 文件绝对路径
        target_dir: 解压目标 (workspace 子目录, 已 mkdir)
        workspace: workspace 根 (用于 quota 检查)

    Returns:
        解压出的文件元信息列表 [{name, path, size}, ...]

    Raises:
        HTTPException(400 zip_slip / 400 zip_bomb / 507 quota_exceeded)
    """
    extracted = []
    decompressed_so_far = 0
    current_workspace_size = _workspace_size_bytes(workspace)  # 不含 target_dir

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            entries = zf.infolist()
            if len(entries) > _ZIP_MAX_ENTRIES:
                raise HTTPException(
                    status_code=400,
                    detail=f"zip 包含过多 entry ({len(entries)} > {_ZIP_MAX_ENTRIES})",
                )

            for entry in entries:
                # 1) zip slip 检查: 拒绝 .. 段 / 绝对路径 / Windows 盘符
                entry_path = Path(entry.filename)
                if entry_path.is_absolute() or any(part == ".." for part in entry_path.parts):
                    raise HTTPException(
                        status_code=400,
                        detail=f"zip slip 检测: 非法 entry {entry.filename!r}",
                    )
                if entry.filename.startswith("\\"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"zip slip 检测 (UNC): 非法 entry {entry.filename!r}",
                    )

                # 2) zip bomb 检查: 单 entry 压缩比 > 100
                if entry.compress_size > 0:
                    ratio = entry.file_size / entry.compress_size
                    if ratio > _ZIP_ENTRY_MAX_RATIO:
                        raise HTTPException(
                            status_code=400,
                            detail=f"zip bomb 检测: {entry.filename!r} 压缩比 {ratio:.0f}:1 超过 {_ZIP_ENTRY_MAX_RATIO}:1",
                        )

                # 3) 累加解压后大小 + quota 检查
                decompressed_so_far += entry.file_size
                if current_workspace_size + decompressed_so_far > QUOTA_BYTES:
                    raise HTTPException(
                        status_code=507,
                        detail=f"解压后超过 session quota: {current_workspace_size + decompressed_so_far} > {QUOTA_BYTES}",
                    )

                # 4) 流式解压 (不调 extractall!)
                #    直接路径 = target_dir / entry.filename
                #    entry_path 是相对路径, target_dir 已在 workspace 内,
                #    is_relative_to 防御 (defense in depth) — 再次确认不越界
                out_path = (target_dir / entry.filename).resolve()
                if not out_path.is_relative_to(target_dir.resolve()):
                    raise HTTPException(
                        status_code=400,
                        detail=f"entry 解析后越界: {entry.filename!r}",
                    )

                if entry.is_dir():
                    out_path.mkdir(parents=True, exist_ok=True)
                    continue

                # 确保父目录存在
                out_path.parent.mkdir(parents=True, exist_ok=True)
                # 流式写 (用 zf.open + shutil.copyfileobj, 不一次性 load)
                with zf.open(entry) as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                extracted.append({
                    "name": out_path.name,
                    "path": out_path.relative_to(workspace).as_posix(),
                    "size": out_path.stat().st_size,
                })
    except HTTPException:
        # 失败回滚: 删掉 target_dir (含已解压的部分内容)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except zipfile.BadZipFile as e:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"zip 文件损坏: {e}")
    except Exception as e:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"解压失败: {e}")

    return extracted


# === POST /upload ===

@router.post("/{session_id}/upload", status_code=201)
async def upload_to_workspace(session_id: str, files: List[UploadFile] = File(...)):
    """上传文件到 session workspace

    Body: multipart/form-data, field name = 'files' (可重复)
    Response 201: {uploaded: [{filename, path, size, uploaded_at}], total_files, total_bytes}
    """
    if not _upload_enabled():
        raise HTTPException(status_code=503, detail="WORKSPACE_UPLOAD_ENABLED=false")

    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session {session_id} not found, 请先通过 POST /api/sessions 创建")

    workspace = workspace_dir(session_id)  # 自动 mkdir
    uploaded_results = []
    uploaded_files: List[tuple] = []  # 跟踪已上传的 tmp 文件, 失败时回滚

    for upload in files:
        try:
            # 1) 清洗文件名
            try:
                clean_name = safe_workspace_filename(upload.filename or "unnamed")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"非法文件名 {upload.filename!r}: {e.user_message if hasattr(e, 'user_message') else str(e)}")

            ext = Path(clean_name).suffix.lower()
            # 注意: .zip 走流式解压通道 (Phase 2b 实现);v1 先按普通文件保存
            # 这里保留 .zip 通过 ALLOWED_EXTENSIONS, 实际解压逻辑下个 phase 加

            # 2) 流式读 + 大小限制
            tmp_path: Optional[Path] = None
            try:
                # 用系统 temp dir 而非 workspace 内的 tmp 路径
                # 原因: 配额检查时 _workspace_size_bytes 算的是 workspace 内文件,
                #       如果 tmp 也在 workspace, 会重复计数 (tmp 自身 + 新增 file_size)
                tmp_fd, tmp_str = tempfile.mkstemp(prefix="ws_upload_", suffix=".tmp")
                tmp_path = Path(tmp_str)
                total_written = 0
                head_bytes = b""
                with os.fdopen(tmp_fd, "wb") as f:
                    while True:
                        chunk = await upload.read(1024 * 64)
                        if not chunk:
                            break
                        total_written += len(chunk)
                        if total_written > MAX_FILE_BYTES:
                            f.close()
                            tmp_path.unlink(missing_ok=True)
                            raise HTTPException(
                                status_code=413,
                                detail=f"文件过大: 超过 {MAX_FILE_BYTES} B 上限 (中途终止)",
                            )
                        if len(head_bytes) < _MAGIC_CHECK_BYTES:
                            need = _MAGIC_CHECK_BYTES - len(head_bytes)
                            head_bytes += chunk[:need]
                        f.write(chunk)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass  # Windows 上 fsync 在某些 fs 上可能不支持

                # 3) 配额检查 (tmp 不在 workspace, 计数准确)
                _check_upload_constraints(ext, total_written, workspace)
                # 4) 魔数校验
                with open(tmp_path, "rb") as f:
                    real_head = f.read(_MAGIC_CHECK_BYTES)
                _check_magic_bytes(ext, real_head)

                # 5) .zip 走流式解压通道, 其他格式走原样保存
                if ext == ".zip":
                    # zip_stem = 文件名去掉 .zip 后缀
                    zip_stem = clean_name[:-len(".zip")]
                    # 子目录用 unique_workspace_target 重名防覆盖
                    target_subdir = unique_workspace_target(workspace, zip_stem)
                    target_subdir.mkdir(parents=True, exist_ok=False)
                    extracted = _extract_zip_streaming(tmp_path, target_subdir, workspace)
                    # zip 文件本身不保留 (只保留解压内容)
                    tmp_path.unlink(missing_ok=True)
                    tmp_path = None
                    now = datetime.now().isoformat(timespec="seconds")
                    for f_meta in extracted:
                        uploaded_results.append({
                            "filename": f_meta["name"],
                            "path": f_meta["path"],
                            "size": f_meta["size"],
                            "uploaded_at": now,
                            "extracted_from": clean_name,  # 每个 entry 都标记来源 zip
                        })
                else:
                    # 原样保存 (非 zip)
                    target = unique_workspace_target(workspace, clean_name)
                    # 用 shutil.move 而不是 os.replace, 因为 tmp 和 target 可能在不同盘
                    shutil.move(str(tmp_path), str(target))
                    tmp_path = None  # move 成功, 标记为不再清理
                    uploaded_files.append((target, total_written))

                    stat = target.stat()
                    uploaded_results.append({
                        "filename": target.name,
                        "path": target.name,  # 相对 workspace
                        "size": stat.st_size,
                        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                    })
            except HTTPException:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise
            except Exception as e:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=f"上传失败: {e}")
        finally:
            await upload.close()

    # 汇总
    return {
        "uploaded": uploaded_results,
        "total_files": len(uploaded_results),
        "total_bytes": sum(r["size"] for r in uploaded_results),
    }


# === GET /workspace ===

@router.get("/{session_id}/workspace")
async def list_workspace(session_id: str):
    """列出 session workspace 内所有文件 (元数据, 不含内容)"""
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    workspace = workspace_dir(session_id)
    files = []
    for entry in sorted(
        workspace.rglob("*"),
        key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name),
    ):
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
            rel = entry.relative_to(workspace).as_posix()
            # 过滤系统隐藏文件、macOS 元数据、缓存目录
            parts = Path(rel).parts
            if any(p.startswith(".") or p in ("__MACOSX", "__pycache__") for p in parts):
                continue
            if rel.startswith(("unzipped/", "style_profiles/", "drafts/")):
                continue
            files.append({
                "name": entry.name,
                "path": rel,  # 相对 workspace 的正斜杠路径
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            })
        except (OSError, ValueError):
            continue

    return {
        "files": files,
        "total_files": len(files),
        "total_bytes": sum(f["size"] for f in files),
    }


@router.get("/{session_id}/workspace/file/{filepath:path}")
async def get_workspace_file(session_id: str, filepath: str):
    """获取 workspace 内单个文件的二进制流内容 (用于图片渲染)"""
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    from urllib.parse import unquote
    filepath_decoded = unquote(filepath)

    try:
        # 使用统一的安全路径解析器
        target = guard.resolve_workspace_path(
            session_id,
            filepath_decoded,
            must_exist=True,
            must_be_file=True,
            allow_symlinks=False,
        )
    except guard.WorkspacePathError as e:
        if e.code == "not_found":
            raise HTTPException(status_code=404, detail=f"文件不存在: {filepath_decoded}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"路径解析失败: {e}")

    # 自动推导 MIME 类型
    import mimetypes
    mime_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        path=target,
        media_type=mime_type or "application/octet-stream",
        filename=target.name,
    )


# === DELETE /workspace/{filename} ===

@router.delete("/{session_id}/workspace/{filename:path}", status_code=204)
async def delete_workspace_file(session_id: str, filename: str):
    """删除 workspace 内单个文件"""
    if not _upload_enabled():
        raise HTTPException(status_code=503, detail="WORKSPACE_UPLOAD_ENABLED=false")

    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    try:
        # 使用统一的安全路径解析器 (防 traversal, 绝对/相对路径越界，支持子目录文件/文件夹)
        target = guard.resolve_workspace_path(
            session_id,
            filename,
            must_exist=True,
            allow_symlinks=False,
        )
    except guard.WorkspacePathError as e:
        if e.code == "not_found":
            raise HTTPException(status_code=404, detail=f"文件不存在: {filename}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"路径解析失败: {e}")

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")

    return None


# === POST /workspace/clear ===

@router.post("/{session_id}/workspace/clear", status_code=204)
async def clear_workspace(session_id: str):
    """清空 workspace (只删文件, 保留目录)

    供前端"重置 workspace"按钮用
    """
    if not _upload_enabled():
        raise HTTPException(status_code=503, detail="WORKSPACE_UPLOAD_ENABLED=false")

    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session {session_id} 不存在")

    workspace = workspace_dir(session_id)
    for entry in workspace.rglob("*"):
        if entry.is_file():
            try:
                entry.unlink()
            except OSError:
                continue
    return None
