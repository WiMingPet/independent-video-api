from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import hashlib
import requests
import time
import logging
import threading
import uuid
import asyncio
from typing import Optional
from models import User, History, VideoTask
from database import engine, get_db, Base
import config
from logging_config import logger, setup_logging
import traceback
import os

# 创建上传目录
UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

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

# ========== 数据库迁移 ==========
from sqlalchemy import text

try:
    with engine.connect() as conn:
        # 检查 history 表是否有 type 字段
        result = conn.execute(text("PRAGMA table_info(history)"))
        columns = [row[1] for row in result]
        if 'type' not in columns:
            conn.execute(text("ALTER TABLE history ADD COLUMN type VARCHAR DEFAULT 'video'"))
            conn.commit()
            print("✅ 添加 type 字段成功")
        else:
            print("✅ type 字段已存在")
except Exception as e:
    print(f"数据库迁移: {e}")
# ========== 迁移结束 ==========

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
        
        # 保存用户上传的图片
        upload_filename = f"video_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 用户上传图片: {phone}, 文件={upload_filename}, 大小={len(image_data)} bytes")

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

        # ========== 第二步：图生视频（可灵3.0） ==========
        logger.info("开始调用可灵3.0生成视频")
        
        video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-3.0"
        
        video_payload = {
            "contents": [
                {
                    "type": "prompt",
                    "text": prompt if prompt else "让图片动起来"
                },
                {
                    "type": "first_frame",
                    "url": edited_image_url
                }
            ],
            "settings": {
                "resolution": "720",
                "duration": duration,
                "audio": "off",
                "multi_shot": False
            },
            "options": {
                "callback_url": "",
                "external_task_id": "",
                "watermark_info": {
                    "enabled": False
                }
            }
        }

        resp2 = requests.post(video_api_url, json=video_payload, headers=headers)
        video_result = resp2.json()
        logger.info(f"可灵3.0视频API响应: {video_result}")

        if video_result.get("code") != 0:
            logger.error(f"视频生成失败: {video_result.get('message')}")
            raise HTTPException(400, video_result.get("message"))

        video_task_id = video_result["data"]["id"]
        logger.info(f"可灵3.0视频任务ID: {video_task_id}")

        for i in range(60):
            time.sleep(5)
            # 使用系统任务ID查询
            status_resp = requests.get(
                f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                headers=headers
            )
            status_data = status_resp.json()
            logger.info(f"视频生成状态检查 {i+1}/60: {status_data}")
            
            if status_data.get("code") != 0:
                continue
            
            data_list = status_data.get("data", [])
            if not data_list:
                continue
            
            task_info = data_list[0]
            task_status = task_info.get("status", "")
            
            if task_status == "succeeded":
                # 从 outputs 中获取视频URL
                outputs = task_info.get("outputs", [])
                video_url = None
                for output in outputs:
                    if output.get("type") == "video":
                        video_url = output.get("url")
                        break
                
                if not video_url:
                    raise HTTPException(400, "未找到视频URL")
                
                user.credits -= cost
                history = History(phone=phone, video_url=video_url, type="video")
                db.add(history)
                db.commit()
                
                logger.info(f"✅ 视频生成成功!")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   视频URL: {video_url}")
                logger.info(f"   剩余余额: {user.credits}")
                
                return {"code": 200, "video_url": video_url, "credits": user.credits}
                
            elif task_status == "failed":
                error_msg = task_info.get("message", "未知错误")
                logger.error(f"视频生成失败: {error_msg}")
                raise HTTPException(400, error_msg)

        logger.error("视频生成超时")
        raise HTTPException(408, "视频生成超时")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"视频生成异常: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"服务器错误: {str(e)}")

# ========== 后台生成视频接口 ==========
@app.post("/video/generate/background")
async def generate_video_background(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    duration: int = Form(5),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"后台视频生成请求: 手机号={phone}, 时长={duration}s")
    
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    
    cost = config.VIDEO_COSTS.get(duration, 30)
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")
    
    # 先扣费
    user.credits -= cost
    db.commit()
    
    # 创建任务ID
    import uuid
    task_id = str(uuid.uuid4())
    
    # 保存任务信息到数据库
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt=prompt,
        duration=duration,
        cost=cost
    )
    db.add(task)
    db.commit()
    
    logger.info(f"后台任务创建成功: {task_id}")
    
    # 启动后台线程处理
    import threading
    image_data = await image.read()
    thread = threading.Thread(
        target=process_video_in_background,
        args=(task_id, phone, image_data, prompt, duration, cost)
    )
    thread.start()
    
    return {
        "code": 200,
        "message": "任务已提交，将在后台生成",
        "task_id": task_id
    }


def process_video_in_background(task_id, phone, image_data, prompt, duration, cost):
    """后台处理视频生成"""
    from database import SessionLocal
    db = SessionLocal()
    
    logger.info(f"🎬 后台视频生成开始: 任务ID={task_id}, 手机号={phone}")
    
    try:
        # 更新任务状态
        task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
        task.status = "processing"
        db.commit()
        logger.info(f"任务 {task_id}: 状态更新为 processing")
        
        # 调用可灵API生成视频
        import base64
        image_b64 = base64.b64encode(image_data).decode()
        
        # 保存用户上传的图片
        upload_filename = f"bg_video_{phone}_{task_id[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 后台视频上传图片: {phone}, 文件={upload_filename}")
        
        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # 图生图
        logger.info(f"任务 {task_id}: 开始图片处理")
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
            raise Exception(edit_result.get("message"))
        
        edit_task_id = edit_result["data"]["task_id"]
        edited_image_url = None
        
        for i in range(30):
            time.sleep(5)
            edit_status = requests.get(
                f"{config.KLING_API_URL}/images/generations/{edit_task_id}",
                headers=headers
            ).json()["data"]
            logger.info(f"任务 {task_id}: 图片处理状态 {i+1}/30: {edit_status['task_status']}")
            if edit_status["task_status"] == "succeed":
                edited_image_url = edit_status["task_result"]["images"][0]["url"]
                logger.info(f"任务 {task_id}: 图片处理成功")
                break
            elif edit_status["task_status"] == "failed":
                raise Exception(edit_status.get("task_status_msg"))
        
        if not edited_image_url:
            raise Exception("图片处理超时")
        
        # 图生视频（可灵3.0）
        logger.info(f"任务 {task_id}: 开始调用可灵3.0生成视频")
        
        video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-3.0"
        
        video_payload = {
            "contents": [
                {
                    "type": "prompt",
                    "text": prompt if prompt else "让图片动起来"
                },
                {
                    "type": "first_frame",
                    "url": edited_image_url
                }
            ],
            "settings": {
                "resolution": "720p",
                "duration": duration,
                "audio": "off",
                "multi_shot": False
            },
            "options": {
                "callback_url": "",
                "external_task_id": task_id,
                "watermark_info": {
                    "enabled": False
                }
            }
        }
        
        resp2 = requests.post(video_api_url, json=video_payload, headers=headers)
        video_result = resp2.json()
        logger.info(f"任务 {task_id}: 可灵3.0响应: {video_result}")
        
        if video_result.get("code") != 0:
            raise Exception(video_result.get("message"))
        
        video_task_id = video_result["data"]["id"]
        
        for i in range(60):
            time.sleep(5)
            status_resp = requests.get(
                f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                headers=headers
            )
            status_data = status_resp.json()
            logger.info(f"任务 {task_id}: 查询响应: {status_data}")
            
            if status_data.get("code") != 0:
                continue
            
            data_list = status_data.get("data", [])
            if not data_list:
                continue
            
            task_info = data_list[0]
            task_status = task_info.get("status", "")
            logger.info(f"任务 {task_id}: 视频生成状态 {i+1}/60: {task_status}")
            
            if task_status == "succeeded":
                outputs = task_info.get("outputs", [])
                video_url = None
                for output in outputs:
                    if output.get("type") == "video":
                        video_url = output.get("url")
                        break
                
                if not video_url:
                    raise Exception("未找到视频URL")
                
                history = History(phone=phone, video_url=video_url, type="video")
                db.add(history)
                
                task.video_url = video_url
                task.status = "completed"
                db.commit()
                
                logger.info(f"✅ 后台视频生成成功!")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   视频URL: {video_url}")
                
                return
                
            elif task_status == "failed":
                error_msg = task_info.get("message", "未知错误")
                raise Exception(error_msg)
        
        raise Exception("视频生成超时")
        
    except Exception as e:
        logger.error(f"❌ 后台视频生成失败: {task_id}, 错误: {str(e)}")
        task.status = "failed"
        task.error_message = str(e)
        db.commit()
        
        user = get_user_by_phone(db, phone)
        if user:
            user.credits += cost
            db.commit()
            logger.info(f"任务 {task_id}: 已退款 {cost} 点给 {phone}")
    finally:
        db.close()


# ========== 查询任务状态 ==========
@app.get("/video/task/{task_id}")
def get_task_status(task_id: str, db: Session = Depends(get_db)):
    logger.info(f"查询任务状态: {task_id}")
    task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
    if not task:
        raise HTTPException(404, "任务不存在")
    
    return {
        "code": 200,
        "status": task.status,
        "video_url": task.video_url,
        "error_message": task.error_message
    }

# ========== 虚拟试穿接口 ==========
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

        cost = 80  # 试穿扣80点
        if user.credits < cost:
            logger.warning(f"虚拟试穿失败: 余额不足 {phone}, 需要{cost}点, 当前{user.credits}点")
            raise HTTPException(403, f"余额不足，需要{cost}点")

        import base64
        model_data = await model_image.read()
        cloth_data = await cloth_image.read()
        model_b64 = base64.b64encode(model_data).decode()
        cloth_b64 = base64.b64encode(cloth_data).decode()
        
        # 保存用户上传的图片
        model_filename = f"tryon_model_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        cloth_filename = f"tryon_cloth_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        
        with open(os.path.join(UPLOAD_DIR, model_filename), "wb") as f:
            f.write(model_data)
        with open(os.path.join(UPLOAD_DIR, cloth_filename), "wb") as f:
            f.write(cloth_data)
        
        logger.info(f"📤 试穿上传图片: {phone}, 模特图={model_filename}, 服装图={cloth_filename}")

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

        logger.info("开始调用可灵API进行虚拟试穿")
        resp = requests.post(f"{config.KLING_API_URL}/images/omni-image", json=payload, headers=headers)
        result = resp.json()
        logger.info(f"可灵试穿API响应: code={result.get('code')}, message={result.get('message')}")

        if result.get("code") != 0:
            logger.error(f"虚拟试穿失败: {result.get('message')}")
            raise HTTPException(400, result.get("message"))

        task_id = result["data"]["task_id"]
        logger.info(f"虚拟试穿任务ID: {task_id}")

        # 轮询结果
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/omni-image/{task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"虚拟试穿状态检查 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                image_url = status_data["task_result"]["images"][0]["url"]
                user.credits -= cost
                db.commit()
                logger.info(f"虚拟试穿成功: {phone}, URL={image_url}, 剩余余额={user.credits}")
                return {"code": 200, "image_url": image_url, "credits": user.credits}
            elif status_data["task_status"] == "failed":
                logger.error(f"虚拟试穿失败: {status_data.get('task_status_msg')}")
                raise HTTPException(400, status_data.get("task_status_msg"))

        logger.error("虚拟试穿超时")
        raise HTTPException(408, "生成超时")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"虚拟试穿异常: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"服务器错误: {str(e)}")

# ========== 虚拟试穿后台生成接口（生成视频） ==========
@app.post("/tryon/background")
async def tryon_background(
    model_image: UploadFile = File(...),
    cloth_image: UploadFile = File(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"后台虚拟试穿请求: 手机号={phone}")
    
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    
    cost = 80  # 试穿扣80点
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")
    
    # 先扣费
    user.credits -= cost
    db.commit()
    
    # 创建任务ID
    task_id = str(uuid.uuid4())
    
    # 保存任务信息
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt="虚拟试穿",
        cost=cost
    )
    db.add(task)
    db.commit()
    
    # 读取图片数据
    model_data = await model_image.read()
    cloth_data = await cloth_image.read()
    
    # 启动后台线程
    thread = threading.Thread(
        target=process_tryon_in_background,
        args=(task_id, phone, model_data, cloth_data, cost)
    )
    thread.start()
    
    logger.info(f"后台试穿任务创建成功: {task_id}")
    
    return {
        "code": 200,
        "message": "任务已提交，将在后台生成",
        "task_id": task_id
    }


def process_tryon_in_background(task_id, phone, model_data, cloth_data, cost):
    """后台处理虚拟试穿，生成视频"""
    from database import SessionLocal
    db = SessionLocal()
    
    logger.info(f"🎬 后台虚拟试穿开始: 任务ID={task_id}, 手机号={phone}")
    
    try:
        # 更新任务状态
        task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
        task.status = "processing"
        db.commit()
        logger.info(f"任务 {task_id}: 状态更新为 processing")
        
        import base64
        model_b64 = base64.b64encode(model_data).decode()
        cloth_b64 = base64.b64encode(cloth_data).decode()
        
        # 保存用户上传的图片
        model_filename = f"bg_tryon_model_{phone}_{task_id[:8]}.jpg"
        cloth_filename = f"bg_tryon_cloth_{phone}_{task_id[:8]}.jpg"
        
        with open(os.path.join(UPLOAD_DIR, model_filename), "wb") as f:
            f.write(model_data)
        with open(os.path.join(UPLOAD_DIR, cloth_filename), "wb") as f:
            f.write(cloth_data)
        
        logger.info(f"📤 后台试穿上传: {phone}, 模特图={model_filename}, 服装图={cloth_filename}")
        
        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # ========== 第一步：生成试穿图片 ==========
        logger.info(f"任务 {task_id}: 开始生成试穿图片")
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
        logger.info(f"任务 {task_id}: 试穿图片API响应 code={result.get('code')}")
        
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        
        tryon_task_id = result["data"]["task_id"]
        tryon_image_url = None
        
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/omni-image/{tryon_task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"任务 {task_id}: 试穿图片生成状态 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                tryon_image_url = status_data["task_result"]["images"][0]["url"]
                logger.info(f"任务 {task_id}: 试穿图片生成成功")
                break
            elif status_data["task_status"] == "failed":
                raise Exception(status_data.get("task_status_msg"))
        
        if not tryon_image_url:
            raise Exception("试穿图片生成超时")
        
        # 图片转视频（可灵3.0）
        logger.info(f"任务 {task_id}: 开始调用可灵3.0生成视频")
        
        video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-3.0"
        
        video_payload = {
            "contents": [
                {
                    "type": "prompt",
                    "text": "让模特自然展示试穿效果，轻微转动身体"
                },
                {
                    "type": "first_frame",
                    "url": tryon_image_url
                }
            ],
            "settings": {
                "resolution": "720p",
                "duration": 5,
                "audio": "off",
                "multi_shot": False
            },
            "options": {
                "callback_url": "",
                "external_task_id": task_id,
                "watermark_info": {
                    "enabled": False
                }
            }
        }
        
        resp2 = requests.post(video_api_url, json=video_payload, headers=headers)
        video_result = resp2.json()
        logger.info(f"任务 {task_id}: 可灵3.0响应: {video_result}")
        
        if video_result.get("code") != 0:
            raise Exception(video_result.get("message"))
        
        video_task_id = video_result["data"]["id"]
        
        for i in range(60):
            time.sleep(5)
            status_resp = requests.get(
                f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                headers=headers
            )
            status_data = status_resp.json()
            logger.info(f"任务 {task_id}: 查询响应: {status_data}")
            
            if status_data.get("code") != 0:
                continue
            
            data_list = status_data.get("data", [])
            if not data_list:
                continue
            
            task_info = data_list[0]
            task_status = task_info.get("status", "")
            logger.info(f"任务 {task_id}: 视频生成状态 {i+1}/60: {task_status}")
            
            if task_status == "succeeded":
                outputs = task_info.get("outputs", [])
                video_url = None
                for output in outputs:
                    if output.get("type") == "video":
                        video_url = output.get("url")
                        break
                
                if not video_url:
                    raise Exception("未找到视频URL")
                
                history = History(phone=phone, video_url=video_url, type="video")
                db.add(history)
                
                task.video_url = video_url
                task.status = "completed"
                db.commit()
                
                logger.info(f"✅ 后台虚拟试穿成功!")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   视频URL: {video_url}")
                
                return
                
            elif task_status == "failed":
                error_msg = task_info.get("message", "未知错误")
                raise Exception(error_msg)
        
        raise Exception("视频生成超时")
        
    except Exception as e:
        logger.error(f"❌ 后台虚拟试穿失败: 任务ID={task_id}, 错误: {str(e)}")
        task.status = "failed"
        task.error_message = str(e)
        db.commit()
        
        user = get_user_by_phone(db, phone)
        if user:
            user.credits += cost
            db.commit()
            logger.info(f"任务 {task_id}: 已退款 {cost} 点给 {phone}")
    finally:
        db.close()

# ========== 图片生成接口（支持多张） ==========
@app.post("/image/generate")
async def generate_images(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    num_images: int = Form(1),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"图片生成请求: 手机号={phone}, 张数={num_images}, 提示词={prompt[:50]}")
    try:
        user = get_user_by_phone(db, phone)
        if not user:
            logger.error(f"图片生成失败: 用户不存在 {phone}")
            raise HTTPException(404, "用户不存在")

        # 每张图片10点
        cost = num_images * 10
        if user.credits < cost:
            logger.warning(f"图片生成失败: 余额不足 {phone}, 需要{cost}点, 当前{user.credits}点")
            raise HTTPException(403, f"余额不足，需要{cost}点")

        import base64
        image_data = await image.read()
        image_b64 = base64.b64encode(image_data).decode()
        
        # 保存用户上传的图片
        upload_filename = f"image_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 图片生成上传: {phone}, 文件={upload_filename}")

        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model_name": "kling-v3",
            "prompt": prompt if prompt else "保持原图不变",
            "image": f"data:image/jpeg;base64,{image_b64}",
            "aspect_ratio": "1:1",
            "n": num_images  # 生成张数
        }

        logger.info(f"开始调用可灵API生成{num_images}张图片")
        resp = requests.post(f"{config.KLING_API_URL}/images/generations", json=payload, headers=headers)
        result = resp.json()
        logger.info(f"可灵图片生成API响应: code={result.get('code')}, message={result.get('message')}")

        if result.get("code") != 0:
            logger.error(f"图片生成失败: {result.get('message')}")
            raise HTTPException(400, result.get("message"))

        task_id = result["data"]["task_id"]
        logger.info(f"图片生成任务ID: {task_id}")

        # 轮询结果
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/generations/{task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"图片生成状态检查 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                # 获取所有生成的图片
                images = status_data["task_result"]["images"]
                image_urls = [img["url"] for img in images]
                
                # 扣费
                user.credits -= cost

                # 保存到历史记录
                for image_url in image_urls:
                    history = History(phone=phone, video_url=image_url, type="image")
                    db.add(history)

                db.commit()
                
                # ========== 添加详细日志 ==========
                logger.info(f"✅ 图片生成成功!")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   图片数量: {len(image_urls)}")
                for i, url in enumerate(image_urls):
                    logger.info(f"   图片{i+1}: {url}")
                logger.info(f"   剩余余额: {user.credits}")
                # ========== 日志结束 ==========
                
                return {
                    "code": 200, 
                    "images": image_urls, 
                    "credits": user.credits,
                    "count": len(image_urls)
                }
            elif status_data["task_status"] == "failed":
                logger.error(f"图片生成失败: {status_data.get('task_status_msg')}")
                raise HTTPException(400, status_data.get("task_status_msg"))

        logger.error("图片生成超时")
        raise HTTPException(408, "生成超时")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"图片生成异常: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"服务器错误: {str(e)}")

# ========== 图片后台生成接口 ==========
@app.post("/image/generate/background")
async def generate_images_background(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    num_images: int = Form(1),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"后台图片生成请求: 手机号={phone}, 张数={num_images}")
    
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    
    cost = num_images * 10
    if user.credits < cost:
        raise HTTPException(403, f"余额不足，需要{cost}点")
    
    # 先扣费
    user.credits -= cost
    db.commit()
    
    # 创建任务ID
    task_id = str(uuid.uuid4())
    
    # 保存任务信息
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt=prompt,
        cost=cost,
        video_url=None  # 图片任务
    )
    db.add(task)
    db.commit()
    
    # 启动后台线程
    image_data = await image.read()
    thread = threading.Thread(
        target=process_images_in_background,
        args=(task_id, phone, image_data, prompt, num_images, cost)
    )
    thread.start()
    
    logger.info(f"后台图片任务创建成功: {task_id}")
    
    return {
        "code": 200,
        "message": "任务已提交，将在后台生成",
        "task_id": task_id
    }


def process_images_in_background(task_id, phone, image_data, prompt, num_images, cost):
    """后台处理图片生成"""
    from database import SessionLocal
    db = SessionLocal()
    
    logger.info(f"🎨 后台图片生成开始: 任务ID={task_id}, 手机号={phone}, 张数={num_images}")
    
    try:
        # 更新任务状态
        task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
        task.status = "processing"
        db.commit()
        logger.info(f"任务 {task_id}: 状态更新为 processing")
        
        # 调用可灵API
        import base64
        image_b64 = base64.b64encode(image_data).decode()
        
        # 保存用户上传的图片
        upload_filename = f"bg_image_{phone}_{task_id[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 后台图片生成上传: {phone}, 文件={upload_filename}")
        
        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model_name": "kling-v3",
            "prompt": prompt if prompt else "保持原图不变",
            "image": f"data:image/jpeg;base64,{image_b64}",
            "aspect_ratio": "1:1",
            "n": num_images
        }
        
        logger.info(f"任务 {task_id}: 开始调用可灵API生成{num_images}张图片")
        resp = requests.post(f"{config.KLING_API_URL}/images/generations", json=payload, headers=headers)
        result = resp.json()
        logger.info(f"任务 {task_id}: 可灵API响应 code={result.get('code')}, message={result.get('message')}")
        
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        
        keling_task_id = result["data"]["task_id"]
        logger.info(f"任务 {task_id}: 可灵任务ID={keling_task_id}")
        
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/generations/{keling_task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"任务 {task_id}: 图片生成状态 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                images = status_data["task_result"]["images"]
                image_urls = [img["url"] for img in images]
                
                # 保存到历史记录
                for image_url in image_urls:
                    history = History(phone=phone, video_url=image_url, type="image")
                    db.add(history)
                
                # 更新任务状态
                task.status = "completed"
                task.video_url = image_urls[0] if image_urls else None
                db.commit()
                
                # ========== 详细成功日志 ==========
                logger.info(f"✅ 后台图片生成成功!")
                logger.info(f"   任务ID: {task_id}")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   图片数量: {len(image_urls)}")
                for i, url in enumerate(image_urls):
                    logger.info(f"   图片{i+1} URL: {url}")
                logger.info(f"   消耗点数: {cost}")
                # ========== 日志结束 ==========
                
                return
                
            elif status_data["task_status"] == "failed":
                logger.error(f"任务 {task_id}: 图片生成失败: {status_data.get('task_status_msg')}")
                raise Exception(status_data.get("task_status_msg"))
        
        raise Exception("图片生成超时")
        
    except Exception as e:
        logger.error(f"❌ 后台图片生成失败: 任务ID={task_id}, 错误: {str(e)}")
        task.status = "failed"
        task.error_message = str(e)
        db.commit()
        
        user = get_user_by_phone(db, phone)
        if user:
            user.credits += cost
            db.commit()
            logger.info(f"任务 {task_id}: 已退款 {cost} 点给 {phone}")
    finally:
        db.close()

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
    logger.info(f"查询历史记录请求: 手机号={phone}")
    try:
        items = db.query(History).filter(History.phone == phone).order_by(History.created_at.desc()).limit(20).all()
        return {
            "code": 200,
            "data": [
                {
                    "id": h.id, 
                    "video_url": h.video_url, 
                    "type": h.type if h.type else "video",  # 处理旧记录
                    "created_at": str(h.created_at)
                }
                for h in items
            ]
        }
    except Exception as e:
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