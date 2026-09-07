from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import hashlib
import requests
import time
from typing import Optional
from models import User, History

from database import engine, get_db, Base
from models import User
import config

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)

# ========== 数据模型 ==========
class LoginRequest(BaseModel):
    phone: str
    password: str

class RegisterRequest(BaseModel):
    phone: str
    password: str

class AddCreditsRequest(BaseModel):
    phone: str
    credits: int
    admin_key: str

# ========== 工具函数 ==========
def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def get_user_by_phone(db: Session, phone: str):
    return db.query(User).filter(User.phone == phone).first()

# ========== 认证接口 ==========
@app.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if get_user_by_phone(db, req.phone):
        raise HTTPException(400, "手机号已注册")
    user = User(phone=req.phone, password=hash_password(req.password), credits=0)
    db.add(user)
    db.commit()
    return {"code": 200, "message": "注册成功"}

@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = get_user_by_phone(db, req.phone)
    if not user or user.password != hash_password(req.password):
        raise HTTPException(400, "手机号或密码错误")
    token = hashlib.sha256(f"{user.id}-{time.time()}".encode()).hexdigest()
    return {"code": 200, "token": token, "credits": user.credits, "phone": user.phone}

# ========== 充值接口（管理员） ==========
@app.post("/admin_add_credits")
def admin_add_credits(req: AddCreditsRequest, db: Session = Depends(get_db)):
    if req.admin_key != config.ADMIN_KEY:
        raise HTTPException(403, "管理员密钥错误")
    user = get_user_by_phone(db, req.phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    user.credits += req.credits
    db.commit()
    return {"code": 200, "message": f"已给{req.phone}充值{req.credits}点", "credits": user.credits}

# ========== 视频生成接口 ==========
@app.post("/video/generate")
async def generate_video(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    duration: int = Form(5),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")

    cost = config.VIDEO_COSTS.get(duration, 30)
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")

    import base64
    image_data = await image.read()
    image_b64 = base64.b64encode(image_data).decode()

    headers = {
        "Authorization": f"Bearer {config.KLING_API_KEY}",
        "Content-Type": "application/json"
    }

    # ========== 第一步：图生图修改内容 ==========
    edit_payload = {
        "model_name": "kling-v3",
        "prompt": prompt if prompt else "保持原图不变",
        "image": f"data:image/jpeg;base64,{image_b64}",
        "aspect_ratio": "1:1",
        "n": 1
    }

    resp = requests.post(f"{config.KLING_API_URL}/images/generations", json=edit_payload, headers=headers)
    edit_result = resp.json()

    if edit_result.get("code") != 0:
        raise HTTPException(400, edit_result.get("message"))

    edit_task_id = edit_result["data"]["task_id"]
    edited_image_url = None

    for _ in range(30):
        time.sleep(5)
        edit_status = requests.get(
            f"{config.KLING_API_URL}/images/generations/{edit_task_id}",
            headers=headers
        ).json()["data"]
        if edit_status["task_status"] == "succeed":
            edited_image_url = edit_status["task_result"]["images"][0]["url"]
            break
        elif edit_status["task_status"] == "failed":
            raise HTTPException(400, edit_status.get("task_status_msg"))

    if not edited_image_url:
        raise HTTPException(408, "图片处理超时")

    # ========== 第二步：图生视频 ==========
    video_payload = {
        "model_name": "kling-v2-6",
        "prompt": prompt if prompt else "让图片动起来",
        "duration": str(duration),
        "mode": "std",
        "with_audio": True,
        "image": edited_image_url
    }

    resp2 = requests.post(f"{config.KLING_API_URL}/videos/image2video", json=video_payload, headers=headers)
    video_result = resp2.json()

    if video_result.get("code") != 0:
        raise HTTPException(400, video_result.get("message"))

    video_task_id = video_result["data"]["task_id"]

    for _ in range(60):
        time.sleep(5)
        video_status = requests.get(
            f"{config.KLING_API_URL}/videos/image2video/{video_task_id}",
            headers=headers
        ).json()["data"]
        if video_status["task_status"] == "succeed":
            video_url = video_status["task_result"]["videos"][0]["url"]
            user.credits -= cost
            history = History(phone=phone, video_url=video_url)
            db.add(history)
            db.commit()
            return {"code": 200, "video_url": video_url, "credits": user.credits}
        elif video_status["task_status"] == "failed":
            raise HTTPException(400, video_status.get("task_status_msg"))

    raise HTTPException(408, "视频生成超时")

@app.post("/tryon")
async def tryon(
    model_image: UploadFile = File(...),
    cloth_image: UploadFile = File(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")

    # 扣费
    cost = 80  # 试穿扣80点，根据你定价改
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")

    import base64
    model_data = await model_image.read()
    cloth_data = await cloth_image.read()
    model_b64 = base64.b64encode(model_data).decode()
    cloth_b64 = base64.b64encode(cloth_data).decode()

    headers = {
        "Authorization": f"Bearer {config.KLING_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model_name": "kling-v3-omni",
        "prompt": "给模特穿上服装，保持姿势和背景不变，服装细节保持",
        "image_list": [
            {"image": f"data:image/jpeg;base64,{model_b64}"},
            {"image": f"data:image/jpeg;base64,{cloth_b64}"}
        ],
        "resolution": "2k",
        "aspect_ratio": "1:1",
        "n": 1
    }

    resp = requests.post(f"{config.KLING_API_URL}/images/omni-image", json=payload, headers=headers)
    result = resp.json()

    if result.get("code") != 0:
        raise HTTPException(400, result.get("message"))

    task_id = result["data"]["task_id"]

    # 轮询
    for _ in range(30):
        time.sleep(5)
        status_resp = requests.get(
            f"{config.KLING_API_URL}/images/omni-image/{task_id}",
            headers=headers
        )
        status_data = status_resp.json()["data"]
        if status_data["task_status"] == "succeed":
            image_url = status_data["task_result"]["images"][0]["url"]
            user.credits -= cost
            db.commit()
            return {"code": 200, "image_url": image_url, "credits": user.credits}
        elif status_data["task_status"] == "failed":
            raise HTTPException(400, status_data.get("task_status_msg"))

    raise HTTPException(408, "生成超时")

# ========== 图生图接口 ==========
@app.post("/image/edit")
async def edit_image(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")

    cost = 20  # 图生图扣20点
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")

    import base64
    image_data = await image.read()
    image_b64 = base64.b64encode(image_data).decode()

    headers = {
        "Authorization": f"Bearer {config.KLING_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model_name": "kling-v3",
        "prompt": prompt,
        "image": f"data:image/jpeg;base64,{image_b64}",
        "aspect_ratio": "1:1",
        "n": 1
    }

    resp = requests.post(f"{config.KLING_API_URL}/images/generations", json=payload, headers=headers)
    result = resp.json()

    if result.get("code") != 0:
        raise HTTPException(400, result.get("message"))

    task_id = result["data"]["task_id"]

    for _ in range(30):
        time.sleep(5)
        status_resp = requests.get(
            f"{config.KLING_API_URL}/images/generations/{task_id}",
            headers=headers
        )
        status_data = status_resp.json()["data"]
        if status_data["task_status"] == "succeed":
            image_url = status_data["task_result"]["images"][0]["url"]
            user.credits -= cost
            db.commit()
            return {"code": 200, "image_url": image_url, "credits": user.credits}
        elif status_data["task_status"] == "failed":
            raise HTTPException(400, status_data.get("task_status_msg"))

    raise HTTPException(408, "生成超时")

# ========== 查询余额 ==========
@app.get("/credits/{phone}")
def get_credits(phone: str, db: Session = Depends(get_db)):
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    return {"phone": phone, "credits": user.credits}

@app.get("/history/{phone}")
def get_history(phone: str, db: Session = Depends(get_db)):
    items = db.query(History).filter(History.phone == phone).order_by(History.created_at.desc()).limit(10).all()
    return {
        "code": 200,
        "data": [
            {"id": h.id, "video_url": h.video_url, "created_at": str(h.created_at)}
            for h in items
        ]
    }