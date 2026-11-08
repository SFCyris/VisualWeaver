# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_projects — the projects API (see projects).

GET is open to every signed-in user (auth.USER_ROUTES); creating, changing and deleting
a project is for administrators (every route not listed there is). Filing a conversation
under a project goes through PATCH /api/sessions/{sid}, the caller's own conversations.
"""
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import appcore, projects

log = logging.getLogger(__name__)


def _store_error(e: Exception):
    raise HTTPException(503, f"Projects could not be stored ({str(e)[:200]})")


@appcore.app.get("/api/projects")
def api_projects():
    """Every project, by name."""
    try:
        return JSONResponse(projects.list_projects())
    except Exception as e:
        _store_error(e)


@appcore.app.post("/api/projects")
def api_create_project(payload: dict):
    """{name, color?, icon?, kbs?, template?, provider?, model?, documents_only?} → the project."""
    try:
        return projects.create(payload)
    except projects.Invalid as e:
        raise HTTPException(400, str(e))
    except projects.Conflict as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        _store_error(e)


@appcore.app.patch("/api/projects/{pid}")
def api_update_project(pid: str, payload: dict):
    """Change some of a project's fields → the project."""
    try:
        p = projects.update(pid, payload)
    except projects.Invalid as e:
        raise HTTPException(400, str(e))
    except projects.Conflict as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        _store_error(e)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


@appcore.app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    """Delete a project. Its conversations are kept, without a project:
    → {"ok": true, "unfiled": <conversations taken out of it>}."""
    try:
        n = projects.delete(pid)
    except Exception as e:
        _store_error(e)
    if n is None:
        raise HTTPException(404, "project not found")
    return {"ok": True, "unfiled": n}
