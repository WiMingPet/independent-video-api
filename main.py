from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import hashlib
import requests
import time
from typing import Optional

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

    cost = config.VIDEO_COSTS.get(duration, 50)
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")

    # 上传图片到可灵（这里简化：直接传base64）
    image_data = await image.read()
    import base64
    image_base64 = base64.b64encode(image_data).decode()

    # 调可灵API
    headers = {
        "Authorization": f"Bearer {config.KLING_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model_name": "kling-v2-6",
        "prompt": prompt or "让图片动起来",
        "duration": str(duration),
        "mode": "std",
        "with_audio": True,
        "image": f"data:image/jpeg;base64,{image_base64}"
    }

    resp = requests.post(f"{config.KLING_API_URL}/videos/image2video", json=payload, headers=headers)
    result = resp.json()

    if result.get("code") != 0:
        raise HTTPException(400, result.get("message"))

    task_id = result["data"]["task_id"]

    # 轮询等待结果
    for _ in range(30):
        time.sleep(5)
        status_resp = requests.get(
            f"{config.KLING_API_URL}/videos/image2video/{task_id}",
            headers=headers
        )
        status_data = status_resp.json()["data"]
        if status_data["task_status"] == "succeed":
            video_url = status_data["task_result"]["videos"][0]["url"]
            # 扣费
            user.credits -= cost
            db.commit()
            return {"code": 200, "video_url": video_url, "credits": user.credits}
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