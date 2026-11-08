# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_media — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from . import appcore, constants, providers

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — LOCAL FILE IMAGE PROXY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import mimetypes

# The one directory images are served from: <data dir>/images. Not the temp directory and
# not the working directory — with sign-in on every signed-in user can call this route,
# and either of those holds far more than tool output.
_ALLOWED_IMAGE_DIRS: list[Path] = [constants.IMAGE_DIR]
try:
    constants.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def image_dir() -> Path:
    return _ALLOWED_IMAGE_DIRS[0]


def image_allowed(path) -> bool:
    """Is ``path`` an existing file inside an allowed directory once every symlink and
    ".." is resolved (so a link or a ../ cannot point outside it)?"""
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    # is_relative_to compares path components: a sibling directory whose name merely
    # begins with an allowed one ("/tmpevil" next to "/tmp") is not inside it.
    return p.is_file() and any(p.is_relative_to(d.resolve()) for d in _ALLOWED_IMAGE_DIRS)


@appcore.app.get("/api/files/image")
async def api_serve_image(path: str):
    """
    Serve an image a tool wrote for an answer. Only files that:
      - Exist on disk
      - Have an image extension (png, jpg, gif, webp, bmp)
      - Are inside <data dir>/images once symlinks and ".." are resolved
    """
    try:
        p = Path(path).resolve()
    except Exception:
        raise HTTPException(400, "Invalid path")

    if p.suffix.lower() not in providers._IMG_EXTS:
        raise HTTPException(400, "Not an image file")

    # One answer for "not there" and "there, but outside the image folder": telling them
    # apart would let any caller probe which files exist anywhere on the disk.
    if not image_allowed(path):
        raise HTTPException(404, "File not found")

    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"
    # identity => GZipMiddleware forwards untouched. Images are already compressed
    # (PNG/JPEG/WebP), so a max-effort gzip pass costs CPU for ~0 byte gain.
    return FileResponse(str(p), media_type=mime,
                        headers={"Content-Encoding": "identity"})

