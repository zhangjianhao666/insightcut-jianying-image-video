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
    voice_type: str = Field(default="", max_length=120)
    catchphrase: str = Field(default="", max_length=200)
    canon: str = Field(default="", max_length=1200)
    visual_identity: str = Field(default="", max_length=1000)
    outfit: str = Field(default="", max_length=450)
    world: str = Field(default="", max_length=600)
    art_style: str = Field(default="", max_length=650)
    camera_style: str = Field(default="", max_length=400)
    negative_rules: str = Field(default="", max_length=400)


@router.get("/ip-profiles")
async def list_ip_profiles():
    profiles = await asyncio.to_thread(ip_library.list_profiles)
    return {"items": [ip_library.public_profile(profile) for profile in profiles]}


@router.post("/ip-profiles", status_code=201)
async def create_ip_profile(data: IpProfilePayload):
    profile = await asyncio.to_thread(ip_library.save_profile, data.model_dump())
    return ip_library.public_profile(profile)


@router.put("/ip-profiles/{ip_id}")
async def update_ip_profile(ip_id: str, data: IpProfilePayload):
    try:
        profile = await asyncio.to_thread(ip_library.save_profile, data.model_dump(), ip_id)
        return ip_library.public_profile(profile)
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
    return {"profile": ip_library.public_profile(profile), "reference_ready": True}


@router.post("/ip-profiles/{ip_id}/voice-reference")
async def upload_ip_voice_reference(ip_id: str, file: UploadFile = File(...)):
    data = await file.read(8 * 1024 * 1024 + 1)
    try:
        profile = await asyncio.to_thread(ip_library.save_voice_reference, ip_id, data)
    except KeyError:
        raise HTTPException(404, "IP 角色不存在") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"profile": ip_library.public_profile(profile)}


@router.delete("/ip-profiles/{ip_id}/voice-reference")
async def remove_ip_voice_reference(ip_id: str):
    try:
        profile = await asyncio.to_thread(ip_library.clear_voice_reference, ip_id)
    except KeyError:
        raise HTTPException(404, "IP 角色不存在") from None
    return {"profile": ip_library.public_profile(profile)}


@router.get("/tasks/{task_id}/ip-profile")
async def get_project_ip(task_id: str):
    if not mysql_client.get_task(task_id):
        raise HTTPException(404, "项目不存在")
    profile = await asyncio.to_thread(ip_library.project_ip, task_id)
    return {"profile": ip_library.public_profile(profile)}
