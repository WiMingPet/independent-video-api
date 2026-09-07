from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import hashlib
import requests
import time
import logging
from typing import Optional
from models import User, History
from database import engine, get_db, Base
import config
from logging_config import logger, setup_logging
import traceback

# 初始化日志
setup_logging()

app = FastAPI()

# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录所有请求"""
    start_time = time.time()
    
    # 记录请求开始
    logger.info(f"收到请求: {request.method} {request.url.path}")
    
    try:
        response = await call_next(request)
        
        # 记录请求完成
        process_time = time.time() - start_time
        logger.info(
            f"请求完成: {request.method} {request.url.path} "
            f"状态码: {response.status_code} "
            f"耗时: {process_time:.2f}s"
        )
        
        return response
    except Exception as e:
        logger.error(f"请求异常: {request.method} {request.url.path} - {str(e)}")
        logger.error(traceback.format_exc())
        raise

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
    logger.info(f"注册请求: 手机号={req.phone}")
    try:
        if get_user_by_phone(db, req.phone):
            logger.warning(f"注册失败: 手机号已存在 {req.phone}")
            raise HTTPException(400, "手机号已注册")
        
        user = User(phone=req.phone, password=hash_password(req.password), credits=0)
        db.add(user)
        db.commit()
        logger.info(f"注册成功: 手机号={req.phone}")
        return {"code": 200, "message": "注册成功"}
    except Exception as e:
        logger.error(f"注册异常: {str(e)}")
        raise

@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    logger.info(f"登录请求: 手机号={req.phone}")
    try:
        user = get_user_by_phone(db, req.phone)
        if not user or user.password != hash_password(req.password):
            logger.warning(f"登录失败: 手机号或密码错误 {req.phone}")
            raise HTTPException(400, "手机号或密码错误")
        
        token = hashlib.sha256(f"{user.id}-{time.time()}".encode()).hexdigest()
        logger.info(f"登录成功: 手机号={req.phone}, 余额={user.credits}")
        return {"code": 200, "token": token, "credits": user.credits, "phone": user.phone}
    except Exception as e:
        logger.error(f"登录异常: {str(e)}")
        raise

# ========== 充值接口（管理员） ==========
@app.post("/admin_add_credits")
def admin_add_credits(req: AddCreditsRequest, db: Session = Depends(get_db)):
    logger.info(f"管理员充值请求: 手机号={req.phone}, 点数={req.credits}")
    try:
        if req.admin_key != config.ADMIN_KEY:
            logger.error(f"管理员充值失败: 密钥错误")
            raise HTTPException(403, "管理员密钥错误")
        
        user = get_user_by_phone(db, req.phone)
        if not user:
            logger.error(f"管理员充值失败: 用户不存在 {req.phone}")
            raise HTTPException(404, "用户不存在")
        
        old_credits = user.credits
        user.credits += req.credits
        db.commit()
        logger.info(f"充值成功: {req.phone} 从 {old_credits} 点增加到 {user.credits} 点")
        return {"code": 200, "message": f"已给{req.phone}充值{req.credits}点", "credits": user.credits}
    except Exception as e:
        logger.error(f"管理员充值异常: {str(e)}")
        raise

# ========== 视频生成接口 ==========
@app.post("/video/generate")
async def generate_video(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    duration: int = Form(5),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"视频生成请求: 手机号={phone}, 时长={duration}s, 提示词={prompt[:50]}")
    try:
        user = get_user_by_phone(db, phone)
        if not user:
            logger.error(f"视频生成失败: 用户不存在 {phone}")
            raise HTTPException(404, "用户不存在")

        cost = config.VIDEO_COSTS.get(duration, 30)
        if user.credits < cost:
            logger.warning(f"视频生成失败: 余额不足 {phone}, 需要{cost}点, 当前{user.credits}点")
            raise HTTPException(403, f"余额不足，需要{cost}点")

        import base64
        image_data = await image.read()
        image_b64 = base64.b64encode(image_data).decode()
        logger.info(f"图片读取成功: 大小={len(image_data)} bytes")

        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }

        # ========== 第一步：图生图修改内容 ==========
        logger.info("开始调用可灵API进行图片处理")
        edit_payload = {
            "model_name": "kling-v3",
            "prompt": prompt if prompt else "保持原图不变",
            "image": f"data:image/jpeg;base64,{image_b64}",
            "aspect_ratio": "1:1",
            "n": 1
        }

        resp = requests.post(f"{config.KLING_API_URL}/images/generations", json=edit_payload, headers=headers)
        edit_result = resp.json()
        logger.info(f"可灵图片API响应: code={edit_result.get('code')}, message={edit_result.get('message')}")

        if edit_result.get("code") != 0:
            logger.error(f"图片处理失败: {edit_result.get('message')}")
            raise HTTPException(400, edit_result.get("message"))

        edit_task_id = edit_result["data"]["task_id"]
        logger.info(f"图片处理任务ID: {edit_task_id}")
        edited_image_url = None

        for i in range(30):
            time.sleep(5)
            edit_status = requests.get(
                f"{config.KLING_API_URL}/images/generations/{edit_task_id}",
                headers=headers
            ).json()["data"]
            logger.info(f"图片处理状态检查 {i+1}/30: {edit_status['task_status']}")
            if edit_status["task_status"] == "succeed":
                edited_image_url = edit_status["task_result"]["images"][0]["url"]
                logger.info("图片处理成功")
                break
            elif edit_status["task_status"] == "failed":
                logger.error(f"图片处理失败: {edit_status.get('task_status_msg')}")
                raise HTTPException(400, edit_status.get("task_status_msg"))

        if not edited_image_url:
            logger.error("图片处理超时")
            raise HTTPException(408, "图片处理超时")

        # ========== 第二步：图生视频 ==========
        logger.info("开始调用可灵API生成视频")
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
        logger.info(f"可灵视频API响应: code={video_result.get('code')}, message={video_result.get('message')}")

        if video_result.get("code") != 0:
            logger.error(f"视频生成失败: {video_result.get('message')}")
            raise HTTPException(400, video_result.get("message"))

        video_task_id = video_result["data"]["task_id"]
        logger.info(f"视频生成任务ID: {video_task_id}")

        for i in range(60):
            time.sleep(5)
            video_status = requests.get(
                f"{config.KLING_API_URL}/videos/image2video/{video_task_id}",
                headers=headers
            ).json()["data"]
            logger.info(f"视频生成状态检查 {i+1}/60: {video_status['task_status']}")
            if video_status["task_status"] == "succeed":
                video_url = video_status["task_result"]["videos"][0]["url"]
                user.credits -= cost
                history = History(phone=phone, video_url=video_url)
                db.add(history)
                db.commit()
                logger.info(f"视频生成成功: {phone}, URL={video_url}, 剩余余额={user.credits}")
                return {"code": 200, "video_url": video_url, "credits": user.credits}
            elif video_status["task_status"] == "failed":
                logger.error(f"视频生成失败: {video_status.get('task_status_msg')}")
                raise HTTPException(400, video_status.get("task_status_msg"))

        logger.error("视频生成超时")
        raise HTTPException(408, "视频生成超时")
    except Exception as e:
        logger.error(f"视频生成异常: {str(e)}")
        logger.error(traceback.format_exc())
        raise

# ========== 其他接口同样添加日志 ==========
@app.post("/tryon")
async def tryon(
    model_image: UploadFile = File(...),
    cloth_image: UploadFile = File(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"虚拟试穿请求: 手机号={phone}")
    try:
        user = get_user_by_phone(db, phone)
        if not user:
            logger.error(f"虚拟试穿失败: 用户不存在 {phone}")
            raise HTTPException(404, "用户不存在")

        cost = 80
        if user.credits < cost:
            logger.warning(f"虚拟试穿失败: 余额不足 {phone}, 需要{cost}点")
            raise HTTPException(403, f"余额不足，需要{cost}点")

        # ... 原有代码 ...
        logger.info(f"虚拟试穿成功: {phone}")
        return {"code": 200, "image_url": image_url, "credits": user.credits}
    except Exception as e:
        logger.error(f"虚拟试穿异常: {str(e)}")
        raise

# ... 其他接口类似添加日志 ...

# ========== 查询余额 ==========
@app.get("/credits/{phone}")
def get_credits(phone: str, db: Session = Depends(get_db)):
    logger.info(f"查询余额: {phone}")
    try:
        user = get_user_by_phone(db, phone)
        if not user:
            logger.warning(f"查询余额失败: 用户不存在 {phone}")
            raise HTTPException(404, "用户不存在")
        logger.info(f"查询余额成功: {phone}, 余额={user.credits}")
        return {"phone": phone, "credits": user.credits}
    except Exception as e:
        logger.error(f"查询余额异常: {str(e)}")
        raise

@app.get("/history/{phone}")
def get_history(phone: str, db: Session = Depends(get_db)):
    # 1. 在函数开始处添加日志 - 记录请求开始
    logger.info(f"查询历史记录请求: 手机号={phone}")
    
    try:
        # 2. 查询数据库
        items = db.query(History).filter(History.phone == phone).order_by(History.created_at.desc()).limit(10).all()
        
        # 3. 记录查询结果数量
        logger.info(f"查询到 {len(items)} 条历史记录")
        
        # 4. 可选：记录每条记录的详细信息
        for item in items:
            logger.info(f"历史记录详情 - ID: {item.id}, 视频URL: {item.video_url}, 创建时间: {item.created_at}")
        
        # 5. 记录查询成功
        logger.info(f"历史记录查询成功: 手机号={phone}, 记录数={len(items)}")
        
        # 6. 返回结果
        return {
            "code": 200,
            "data": [
                {"id": h.id, "video_url": h.video_url, "created_at": str(h.created_at)}
                for h in items
            ]
        }
        
    except Exception as e:
        # 7. 记录异常
        logger.error(f"查询历史记录失败: 手机号={phone}, 错误={str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"查询失败: {str(e)}")

# ========== 删除历史记录 ==========
@app.delete("/history/delete/{history_id}")
def delete_history(history_id: int, db: Session = Depends(get_db)):
    logger.info(f"删除历史记录请求: ID={history_id}")
    try:
        history = db.query(History).filter(History.id == history_id).first()
        if not history:
            logger.warning(f"删除失败: 记录不存在 ID={history_id}")
            raise HTTPException(404, "记录不存在")
        
        # 记录删除前的信息
        logger.info(f"准备删除记录 - ID: {history.id}, 手机号: {history.phone}, 视频URL: {history.video_url}")
        
        db.delete(history)
        db.commit()
        logger.info(f"删除成功: ID={history_id}")
        return {"code": 200, "message": "删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除历史记录异常: {str(e)}")
        logger.error(traceback.format_exc())
        db.rollback()
        raise HTTPException(500, f"删除失败: {str(e)}")