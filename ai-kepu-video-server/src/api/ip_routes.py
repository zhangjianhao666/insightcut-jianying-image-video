"""Local storytelling IP profiles shared by multiple videos."""
import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.database import mysql_client
from src.text import ip_library


router = APIRouter(prefix="/ai/native/video/kepu", tags=["ip-library"])


class IpProfilePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    persona: str = Field(default="", max_length=900)
    voice_style: str = Field(default="", max_length=650)
    visual_identity: str = Field(default="", max_length=1000)
    outfit: str = Field(default="", max_length=450)
    world: str = Field(default="", max_length=600)
    art_style: str = Field(default="", max_length=650)
    camera_style: str = Field(default="", max_length=400)
    negative_rules: str = Field(default="", max_length=400)


@router.get("/ip-profiles")
async def list_ip_profiles():
    return {"items": await asyncio.to_thread(ip_library.list_profiles)}


@router.post("/ip-profiles", status_code=201)
async def create_ip_profile(data: IpProfilePayload):
    return await asyncio.to_thread(ip_library.save_profile, data.model_dump())


@router.put("/ip-profiles/{ip_id}")
async def update_ip_profile(ip_id: str, data: IpProfilePayload):
    try:
        return await asyncio.to_thread(ip_library.save_profile, data.model_dump(), ip_id)
    except KeyError:
        raise HTTPException(404, "IP 角色不存在") from None


@router.post("/ip-profiles/{ip_id}/reference")
async def upload_ip_reference(ip_id: str, file: UploadFile = File(...)):
    # Limit read without relying on Content-Length; content is verified by PIL.
    data = await file.read(20 * 1024 * 1024 + 1)
    try:
        profile = await asyncio.to_thread(ip_library.save_reference, ip_id, data)
    except KeyError:
        raise HTTPException(404, "IP 角色不存在") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"profile": profile, "reference_ready": True}


@router.get("/tasks/{task_id}/ip-profile")
async def get_project_ip(task_id: str):
    if not mysql_client.get_task(task_id):
        raise HTTPException(404, "项目不存在")
    return {"profile": await asyncio.to_thread(ip_library.project_ip, task_id)}
