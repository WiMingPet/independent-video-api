from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from alipay_pay import create_page_payment, create_wap_payment, verify_notification
from datetime import timedelta
from models import User, History, VideoTask, Subscription
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse 
from sqlalchemy.orm import Session
from pydantic import BaseModel
import hashlib
import requests
import time
import logging
import threading
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from models import User, History, VideoTask
from database import engine, get_db, Base
import config
from logging_config import logger, setup_logging
import traceback
import os
import hashlib

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

# ========== 迁移 request_hash 字段 ==========
try:
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(video_tasks)"))
        columns = [row[1] for row in result]
        if 'request_hash' not in columns:
            conn.execute(text("ALTER TABLE video_tasks ADD COLUMN request_hash VARCHAR"))
            conn.commit()
            print("✅ 添加 request_hash 字段成功")
        else:
            print("✅ request_hash 字段已存在")
except Exception as e:
    print(f"request_hash 迁移: {e}")
# ========== request_hash 迁移结束 ==========

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

def generate_request_hash(phone: str, prompt: str, duration: int, audio: str) -> str:
    """生成请求去重哈希"""
    raw = f"{phone}_{prompt}_{duration}_{audio}"
    return hashlib.md5(raw.encode()).hexdigest()

def enhance_prompt(prompt: str) -> str:
    """增强提示词，提升动作精准度，不添加固定限制"""
    if not prompt:
        return "让人物自然微动，动作流畅"
    
    # 只在用户没有明确描述节奏时补充
    if any(word in prompt for word in ["转身", "走动", "跳舞", "挥手", "跑", "跳", "蹲", "坐", "躺"]):
        if "缓慢" not in prompt and "自然" not in prompt and "快速" not in prompt:
            prompt += "，动作流畅自然"
    
    # 说话类补充口型同步
    if ("说" in prompt or "唱" in prompt) and "口型" not in prompt:
        prompt += "，口型与语音同步"
    
    # 多个动作时补充顺序
    if prompt.count("，") >= 2 and "顺序" not in prompt and "然后" not in prompt:
        prompt += "，动作按描述顺序依次完成"
    
    return prompt

# 套餐定义
SUBSCRIPTION_PLANS = {
    "plan_1000_a": {"name": "1000元套餐A", "amount": 1000.0, "video_silent": 300, "video_audio": 0, "images": 1000},
    "plan_1000_b": {"name": "1000元套餐B", "amount": 1000.0, "video_silent": 50, "video_audio": 50, "images": 500},
    "plan_500_a": {"name": "500元套餐A", "amount": 500.0, "video_silent": 150, "video_audio": 0, "images": 500},
    "plan_500_b": {"name": "500元套餐B", "amount": 500.0, "video_silent": 50, "video_audio": 20, "images": 300},
}

def get_active_subscription(db: Session, phone: str):
    """获取用户有效套餐"""
    now = datetime.utcnow()
    return db.query(Subscription).filter(
        Subscription.phone == phone,
        Subscription.status == "active",
        Subscription.end_date > now
    ).first()

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

@app.post("/subscription/create")
async def create_subscription_payment(
    phone: str = Form(...),
    plan: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    
    if plan not in SUBSCRIPTION_PLANS:
        raise HTTPException(400, "无效的套餐")
    
    plan_info = SUBSCRIPTION_PLANS[plan]
    order_id = f"SUB_{phone}_{plan}_{int(time.time())}"
    pay_url = create_wap_payment(order_id, plan_info["amount"], plan_info["name"])
    
    logger.info(f"套餐订单创建: {order_id}, 套餐: {plan_info['name']}")
    
    return {"code": 200, "pay_url": pay_url, "order_id": order_id}

# ========== 视频生成接口 ==========
@app.post("/video/generate")
async def generate_video(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    duration: int = Form(5),
    audio: str = Form("off"),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"视频生成请求: 手机号={phone}, 时长={duration}s, 音频={audio}, 提示词={prompt[:50]}")
    try:
        user = get_user_by_phone(db, phone)
        if not user:
            raise HTTPException(404, "用户不存在")

        # ========== 防重复提交检查 ==========
        request_hash = generate_request_hash(phone, prompt or "", duration, audio)
        recent_task = db.query(VideoTask).filter(
            VideoTask.phone == phone,
            VideoTask.request_hash == request_hash,
            VideoTask.status.in_(["pending", "processing"]),
            VideoTask.created_at >= datetime.utcnow() - timedelta(minutes=5)
        ).first()

        if recent_task:
            logger.warning(f"检测到重复提交: {phone}, 任务ID={recent_task.task_id}")
            raise HTTPException(429, "相同任务正在处理中，请勿重复提交")
        # ========== 防重复检查结束 ==========

        # 检查是否有有效套餐
        sub = get_active_subscription(db, phone)

        if sub:
            if duration == 5:
                if audio == "native":
                    if sub.video_audio_limit > 0 and sub.video_audio_used < sub.video_audio_limit:
                        sub.video_audio_used += 1
                        cost = 0
                    else:
                        cost = config.VIDEO_COSTS_AUDIO.get(5, 70)
                        if user.credits < cost:
                            raise HTTPException(403, f"余额不足，需要{cost}点")
                        user.credits -= cost
                else:
                    if sub.video_silent_limit > 0 and sub.video_silent_used < sub.video_silent_limit:
                        sub.video_silent_used += 1
                        cost = 0
                    else:
                        cost = config.VIDEO_COSTS.get(5, 50)
                        if user.credits < cost:
                            raise HTTPException(403, f"余额不足，需要{cost}点")
                        user.credits -= cost
            else:
                if audio == "native":
                    cost = config.VIDEO_COSTS_AUDIO.get(duration, 70)
                else:
                    cost = config.VIDEO_COSTS.get(duration, 50)
                if user.credits < cost:
                    raise HTTPException(403, f"余额不足，需要{cost}点")
                user.credits -= cost

            db.commit()
        else:
            if audio == "native":
                cost = config.VIDEO_COSTS_AUDIO.get(duration, 70)
            else:
                cost = config.VIDEO_COSTS.get(duration, 50)
            if user.credits < cost:
                raise HTTPException(403, f"余额不足，需要{cost}点")
            user.credits -= cost
            db.commit()

        import base64
        image_data = await image.read()
        image_b64 = base64.b64encode(image_data).decode()

        upload_filename = f"video_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)

        logger.info(f"📤 用户上传图片: {phone}")
        logger.info(f"   图片URL: https://video-api.lingjing-media.com/uploads/{upload_filename}")

        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }

        logger.info(f"🎬 视频生成开始 - 手机号: {phone}, 音频: {audio}, 提示词: {prompt if prompt else '让图片动起来'}")

        if audio == "native":
            video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-3.0"
            video_payload = {
                "contents": [
                    {"type": "prompt", "text": enhance_prompt(prompt),
                    {"type": "first_frame", "url": f"data:image/jpeg;base64,{image_b64}"}
                ],
                "settings": {
                    "resolution": "720p",
                    "duration": duration,
                    "audio": "native",
                    "multi_shot": False
                },
                "options": {
                    "callback_url": "",
                    "external_task_id": "",
                    "watermark_info": {"enabled": False}
                }
            }
        else:
            video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-2.6"
            video_payload = {
                "contents": [
                    {"type": "prompt", "text": enhance_prompt(prompt),
                    {"type": "first_frame", "url": f"data:image/jpeg;base64,{image_b64}"}
                ],
                "settings": {
                    "audio": "off",
                    "resolution": "720p",
                    "duration": duration
                },
                "options": {
                    "callback_url": "",
                    "external_task_id": "",
                    "watermark_info": {"enabled": False}
                }
            }

        resp2 = requests.post(video_api_url, json=video_payload, headers=headers)
        video_result = resp2.json()
        logger.info(f"可灵API响应: {video_result}")

        if video_result.get("code") != 0:
            logger.error(f"可灵API错误: {video_result}")
            raise HTTPException(400, video_result.get("message"))

        video_task_id = video_result["data"]["id"]

        for i in range(60):
            time.sleep(5)
            status_resp = requests.get(
                f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                headers=headers
            )
            status_data = status_resp.json()

            data_list = status_data.get("data", [])
            if not data_list:
                continue

            task_info = data_list[0]
            task_status = task_info.get("status", "")

            if task_status == "succeeded":
                outputs = task_info.get("outputs", [])
                video_url = None
                for output in outputs:
                    if output.get("type") == "video":
                        video_url = output.get("url")
                        break

                if not video_url:
                    raise HTTPException(400, "未找到视频URL")

                history = History(phone=phone, video_url=video_url, type="video")
                db.add(history)
                db.commit()

                logger.info(f"✅ 视频生成成功 - 手机号: {phone}, 视频URL: {video_url}")
                return {"code": 200, "video_url": video_url, "credits": user.credits}

            elif task_status == "failed":
                error_msg = task_info.get("message", "未知错误")
                raise HTTPException(400, error_msg)

        raise HTTPException(408, "视频生成超时")

    except HTTPException:
        # 如果是HTTPException（如400、403），也需要退款
        if cost > 0:
            user.credits += cost
            db.commit()
            logger.info(f"视频生成失败，退款 {cost} 点给 {phone}")
        else:
            sub = get_active_subscription(db, phone)
            if sub:
                if audio == "native":
                    sub.video_audio_used = max(0, sub.video_audio_used - 1)
                else:
                    sub.video_silent_used = max(0, sub.video_silent_used - 1)
                db.commit()
                logger.info(f"视频生成失败，恢复套餐次数")
        raise
    except Exception as e:
        # 退款
        if cost > 0:
            user.credits += cost
            db.commit()
            logger.info(f"视频生成失败，退款 {cost} 点给 {phone}")
        else:
            sub = get_active_subscription(db, phone)
            if sub:
                if audio == "native":
                    sub.video_audio_used = max(0, sub.video_audio_used - 1)
                else:
                    sub.video_silent_used = max(0, sub.video_silent_used - 1)
                db.commit()
                logger.info(f"视频生成失败，恢复套餐次数")
        
        logger.error(f"视频生成异常: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"服务器错误: {str(e)}")

# ========== 后台生成视频接口 ==========
@app.post("/video/generate/background")
async def generate_video_background(
    image: UploadFile = File(...),
    prompt: str = Form(""),
    duration: int = Form(5),
    audio: str = Form("off"),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    logger.info(f"后台视频生成请求: 手机号={phone}, 时长={duration}s, 音频={audio}")

    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")

    sub = get_active_subscription(db, phone)

    if sub:
        if duration == 5:
            if audio == "native":
                if sub.video_audio_limit > 0 and sub.video_audio_used < sub.video_audio_limit:
                    sub.video_audio_used += 1
                    cost = 0
                else:
                    cost = config.VIDEO_COSTS_AUDIO.get(5, 70)
                    if user.credits < cost:
                        raise HTTPException(403, f"余额不足，需要{cost}点")
                    user.credits -= cost
            else:
                if sub.video_silent_limit > 0 and sub.video_silent_used < sub.video_silent_limit:
                    sub.video_silent_used += 1
                    cost = 0
                else:
                    cost = config.VIDEO_COSTS.get(5, 50)
                    if user.credits < cost:
                        raise HTTPException(403, f"余额不足，需要{cost}点")
                    user.credits -= cost
        else:
            if audio == "native":
                cost = config.VIDEO_COSTS_AUDIO.get(duration, 70)
            else:
                cost = config.VIDEO_COSTS.get(duration, 50)
            if user.credits < cost:
                raise HTTPException(403, f"余额不足，需要{cost}点")
            user.credits -= cost

        db.commit()
    else:
        if audio == "native":
            cost = config.VIDEO_COSTS_AUDIO.get(duration, 70)
        else:
            cost = config.VIDEO_COSTS.get(duration, 50)
        if user.credits < cost:
            raise HTTPException(403, f"余额不足，需要{cost}点")
        user.credits -= cost
        db.commit()
    
    # 防重复检查
    request_hash = generate_request_hash(phone, prompt or "", duration, audio)
    recent_task = db.query(VideoTask).filter(
        VideoTask.phone == phone,
        VideoTask.request_hash == request_hash,
        VideoTask.status.in_(["pending", "processing"]),
        VideoTask.created_at >= datetime.utcnow() - timedelta(minutes=5)
    ).first()

    if recent_task:
        raise HTTPException(429, "相同任务正在处理中，请勿重复提交")

    task_id = str(uuid.uuid4())
    
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt="[视频]" + (prompt or ""),
        duration=duration,
        cost=cost,
        request_hash=request_hash
    )
    db.add(task)
    db.commit()
    
    logger.info(f"后台任务创建成功: {task_id}")
    
    image_data = await image.read()
    thread = threading.Thread(
        target=process_video_in_background,
        args=(task_id, phone, image_data, prompt, duration, audio, cost)
    )
    thread.start()
    
    return {
        "code": 200,
        "message": "任务已提交，将在后台生成",
        "task_id": task_id
    }


def process_video_in_background(task_id, phone, image_data, prompt, duration, audio, cost):
    """后台处理视频生成"""
    from database import SessionLocal
    db = SessionLocal()
    
    logger.info(f"🎬 后台视频生成开始: 任务ID={task_id}, 手机号={phone}, 音频={audio}")
    
    try:
        task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
        task.status = "processing"
        db.commit()
        
        import base64
        image_b64 = base64.b64encode(image_data).decode()
        
        upload_filename = f"bg_video_{phone}_{task_id[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 用户上传图片: {phone}")
        logger.info(f"   图片URL: https://video-api.lingjing-media.com/uploads/{upload_filename}")
        
        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        logger.info(f"🎬 开始生成视频 - 手机号: {phone}, 提示词: {prompt if prompt else '让图片动起来'}")
        
        if audio == "native":
            video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-3.0"
            video_payload = {
                "contents": [
                    {
                        "type": "prompt",
                        "text": enhance_prompt(prompt),
                    },
                    {
                        "type": "first_frame",
                        "url": f"data:image/jpeg;base64,{image_b64}"
                    }
                ],
                "settings": {
                    "resolution": "720p",
                    "duration": duration,
                    "audio": "native",
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
        else:
            video_api_url = "https://api-beijing.klingai.com/image-to-video/kling-2.6"
            video_payload = {
                "contents": [
                    {
                        "type": "prompt",
                        "text": enhance_prompt(prompt),
                    },
                    {
                        "type": "first_frame",
                        "url": f"data:image/jpeg;base64,{image_b64}"
                    }
                ],
                "settings": {
                    "audio": "off",
                    "resolution": "720p",
                    "duration": duration
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
        logger.info(f"可灵响应: {video_result}")

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
            
            data_list = status_data.get("data", [])
            if not data_list:
                continue
            
            task_info = data_list[0]
            task_status = task_info.get("status", "")
            
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
                
                logger.info(f"✅ 后台视频生成成功 - 手机号: {phone}, 视频URL: {video_url}")
                return
                
            elif task_status == "failed":
                error_msg = task_info.get("message", "未知错误")
                raise Exception(error_msg)

        raise Exception("视频生成超时")
        
    except Exception as e:
        logger.error(f"❌ 后台视频生成失败: {task_id}, 错误: {str(e)}")
        
        # ========== 退款前再确认一次任务状态 ==========
        final_status = None
        try:
            if 'video_task_id' in locals() and video_task_id:
                confirm_resp = requests.get(
                    f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                    headers={"Authorization": f"Bearer {config.KLING_API_KEY}"}
                )
                confirm_data = confirm_resp.json()
                data_list = confirm_data.get("data", [])
                if data_list:
                    final_status = data_list[0].get("status", "")
                    logger.info(f"任务 {task_id}: 退款前确认状态={final_status}")
                    
                    # 如果实际已成功，不退款，保存视频
                    if final_status == "succeeded":
                        outputs = data_list[0].get("outputs", [])
                        for output in outputs:
                            if output.get("type") == "video":
                                video_url = output.get("url")
                                history = History(phone=phone, video_url=video_url, type="video")
                                db.add(history)
                                task.video_url = video_url
                                task.status = "completed"
                                db.commit()
                                logger.info(f"✅ 任务 {task_id}: 退款前确认成功，已保存视频 {video_url}")
                                return
        except Exception as confirm_err:
            logger.error(f"任务 {task_id}: 退款前确认失败: {str(confirm_err)}")
        # ========== 确认结束 ==========
        
        # 确认不是成功，执行退款
        task.status = "failed"
        task.error_message = str(e)
        db.commit()
        
        if cost > 0:
            # 退还点数
            user = get_user_by_phone(db, phone)
            if user:
                user.credits += cost
                db.commit()
                logger.info(f"任务 {task_id}: 已退款 {cost} 点给 {phone}")
        else:
            # 恢复套餐次数
            sub = get_active_subscription(db, phone)
            if sub:
                if audio == "native":
                    sub.video_audio_used = max(0, sub.video_audio_used - 1)
                else:
                    sub.video_silent_used = max(0, sub.video_silent_used - 1)
                db.commit()
                logger.info(f"任务 {task_id}: 已恢复套餐次数")
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

@app.get("/tasks/pending/{phone}")
def get_pending_tasks(phone: str, db: Session = Depends(get_db)):
    tasks = db.query(VideoTask).filter(
        VideoTask.phone == phone,
        VideoTask.status.in_(["pending", "processing"])
    ).order_by(VideoTask.created_at.desc()).limit(5).all()
    
    result = []
    for t in tasks:
        task_type = "video"
        if t.prompt and t.prompt.startswith("[试穿]"):
            task_type = "tryon"
        elif t.prompt and t.prompt.startswith("[图片]"):
            task_type = "image"
        
        result.append({
            "task_id": t.task_id,
            "status": t.status,
            "prompt": t.prompt,
            "duration": t.duration,
            "task_type": task_type,
            "created_at": str(t.created_at)
        })
    
    return {"code": 200, "data": result}

# ========== 虚拟试穿接口（生成视频） ==========
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
        
        logger.info(f"📤 试穿上传图片: {phone}")
        logger.info(f"   模特图URL: https://video-api.lingjing-media.com/uploads/{model_filename}")
        logger.info(f"   服装图URL: https://video-api.lingjing-media.com/uploads/{cloth_filename}")

        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # ========== 第一步：生成试穿图片 ==========
        logger.info("开始调用可灵API生成试穿图片")
        payload = {
            "model_name": "kling-v3-omni",
            "prompt": enhance_prompt("给模特穿上服装，服装细节保持"),
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
        logger.info(f"可灵试穿API响应: code={result.get('code')}, message={result.get('message')}")

        if result.get("code") != 0:
            logger.error(f"试穿图片生成失败: {result.get('message')}")
            raise HTTPException(400, result.get("message"))

        tryon_task_id = result["data"]["task_id"]
        logger.info(f"试穿图片任务ID: {tryon_task_id}")
        tryon_image_url = None

        # 轮询试穿图片结果
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/omni-image/{tryon_task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"试穿图片生成状态 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                tryon_image_url = status_data["task_result"]["images"][0]["url"]
                logger.info("试穿图片生成成功")
                break
            elif status_data["task_status"] == "failed":
                logger.error(f"试穿图片生成失败: {status_data.get('task_status_msg')}")
                raise HTTPException(400, status_data.get("task_status_msg"))

        if not tryon_image_url:
            logger.error("试穿图片生成超时")
            raise HTTPException(408, "试穿图片生成超时")

        # ========== 第二步：图片转视频（可灵3.0） ==========
        logger.info("开始调用可灵3.0生成试穿视频")
        
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
                "external_task_id": f"tryon_{phone}_{int(time.time())}",
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

        # 轮询视频生成结果
        for i in range(60):
            time.sleep(5)
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
                
                logger.info(f"✅ 虚拟试穿成功!")
                logger.info(f"   手机号: {phone}")
                logger.info(f"   试穿图片: {tryon_image_url}")
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
    request_hash = generate_request_hash(phone, "", 0, "")
    
    # 保存任务信息
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt="[试穿]",
        duration=0,
        cost=cost,
        request_hash=request_hash
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
            "prompt": enhance_prompt("给模特穿上服装，服装细节保持"),
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
        
        # ========== 退款前再确认一次 ==========
        try:
            if 'video_task_id' in locals() and video_task_id:
                confirm_resp = requests.get(
                    f"https://api-beijing.klingai.com/tasks?task_ids={video_task_id}",
                    headers={"Authorization": f"Bearer {config.KLING_API_KEY}"}
                )
                confirm_data = confirm_resp.json()
                data_list = confirm_data.get("data", [])
                if data_list:
                    confirm_status = data_list[0].get("status", "")
                    logger.info(f"任务 {task_id}: 退款前确认状态={confirm_status}")
                    
                    if confirm_status == "succeeded":
                        outputs = data_list[0].get("outputs", [])
                        for output in outputs:
                            if output.get("type") == "video":
                                video_url = output.get("url")
                                history = History(phone=phone, video_url=video_url, type="video")
                                db.add(history)
                                task.video_url = video_url
                                task.status = "completed"
                                db.commit()
                                logger.info(f"✅ 任务 {task_id}: 退款前确认成功，已保存试穿视频 {video_url}")
                                return
        except Exception as confirm_err:
            logger.error(f"任务 {task_id}: 退款前确认失败: {str(confirm_err)}")
        # ========== 确认结束 ==========
        
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

        sub = get_active_subscription(db, phone)
        
        if sub:
            remaining = sub.image_limit - sub.image_used
            if remaining < num_images:
                raise HTTPException(403, f"套餐图片剩余不足，剩余{remaining}张")
            sub.image_used += num_images
            cost = 0
            db.commit()
        else:
            cost = num_images * 10
            if user.credits < cost:
                raise HTTPException(403, f"余额不足，需要{cost}点")
            user.credits -= cost
            db.commit()

        import base64
        image_data = await image.read()
        image_b64 = base64.b64encode(image_data).decode()
        
        # 保存用户上传的图片
        upload_filename = f"image_{phone}_{uuid.uuid4().hex[:8]}.jpg"
        upload_path = os.path.join(UPLOAD_DIR, upload_filename)
        with open(upload_path, "wb") as f:
            f.write(image_data)
        
        logger.info(f"📤 图片生成上传: {phone}")
        logger.info(f"   图片URL: https://video-api.lingjing-media.com/uploads/{upload_filename}")

        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model_name": "kling-v3-omni",
            "prompt": enhance_prompt(prompt) if prompt else "保持原图不变，保留人物面部特征、五官、发型、服装细节",
            "image_list": [
                {"image": f"data:image/jpeg;base64,{image_b64}"}
            ],
            "resolution": "1k",
            "n": num_images,
            "aspect_ratio": "1:1"
        }

        logger.info(f"开始调用可灵Omni API生成{num_images}张图片")
        resp = requests.post(f"{config.KLING_API_URL}/images/omni-image", json=payload, headers=headers)
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
                f"{config.KLING_API_URL}/images/omni-image/{task_id}",
                headers=headers
            )
            status_data = status_resp.json()["data"]
            logger.info(f"图片生成状态检查 {i+1}/30: {status_data['task_status']}")
            
            if status_data["task_status"] == "succeed":
                # 获取所有生成的图片
                images = status_data["task_result"]["images"]
                image_urls = [img["url"] for img in images]
                

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
        # 恢复套餐次数或退款
        if sub and cost == 0:
            sub.image_used = max(0, sub.image_used - num_images)
            db.commit()
        elif cost > 0:
            user.credits += cost
            db.commit()
        raise
    except Exception as e:
        # 恢复套餐次数或退款
        if sub and cost == 0:
            sub.image_used = max(0, sub.image_used - num_images)
            db.commit()
        elif cost > 0:
            user.credits += cost
            db.commit()
        logger.error(f"图片生成异常: {str(e)}")
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
    
    # 检查是否有有效套餐
    sub = get_active_subscription(db, phone)
    
    if sub:
        # 套餐用户：检查剩余图片次数
        remaining = sub.image_limit - sub.image_used
        if remaining < num_images:
            raise HTTPException(403, f"套餐图片剩余不足，剩余{remaining}张")
        sub.image_used += num_images
        cost = 0
        db.commit()
        logger.info(f"使用套餐生成图片: {phone}, 本次{num_images}张, 剩余{remaining - num_images}张")
    else:
        # 非套餐用户：正常扣点数
        cost = num_images * 10
        if user.credits < cost:
            raise HTTPException(403, f"余额不足，需要{cost}点")
        user.credits -= cost
        db.commit()
    
    # 创建任务ID
    task_id = str(uuid.uuid4())
    request_hash = generate_request_hash(phone, prompt or "", 0, "")
    
    # 保存任务信息
    task = VideoTask(
        task_id=task_id,
        phone=phone,
        status="pending",
        prompt="[图片]" + (prompt or ""),
        duration=0,
        cost=cost,
        request_hash=request_hash
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
            "model_name": "kling-v3-omni",
            "prompt": enhance_prompt(prompt) if prompt else "保持原图不变，保留人物面部特征、五官、发型、服装细节",
            "image_list": [
                {"image": f"data:image/jpeg;base64,{image_b64}"}
            ],
            "resolution": "1k",
            "n": num_images,
            "aspect_ratio": "1:1"
        }
        
        logger.info(f"任务 {task_id}: 开始调用可灵Omni API生成{num_images}张图片")
        resp = requests.post(f"{config.KLING_API_URL}/images/omni-image", json=payload, headers=headers)
        result = resp.json()
        logger.info(f"任务 {task_id}: 可灵API响应 code={result.get('code')}, message={result.get('message')}")
        
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        
        keling_task_id = result["data"]["task_id"]
        logger.info(f"任务 {task_id}: 可灵任务ID={keling_task_id}")
        
        for i in range(30):
            time.sleep(5)
            status_resp = requests.get(
                f"{config.KLING_API_URL}/images/omni-image/{keling_task_id}",
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
        
        # ========== 退款前再确认一次 ==========
        try:
            if 'keling_task_id' in locals() and keling_task_id:
                confirm_resp = requests.get(
                    f"{config.KLING_API_URL}/images/omni-image/{keling_task_id}",
                    headers={"Authorization": f"Bearer {config.KLING_API_KEY}"}
                )
                confirm_data = confirm_resp.json()
                confirm_status = confirm_data.get("data", {}).get("task_status", "")
                logger.info(f"任务 {task_id}: 退款前确认状态={confirm_status}")
                
                if confirm_status == "succeed":
                    images = confirm_data["data"]["task_result"]["images"]
                    for img in images:
                        history = History(phone=phone, video_url=img["url"], type="image")
                        db.add(history)
                    task.status = "completed"
                    db.commit()
                    logger.info(f"✅ 任务 {task_id}: 退款前确认成功，已保存图片，不退款")
                    return
        except Exception as confirm_err:
            logger.error(f"任务 {task_id}: 退款前确认失败: {str(confirm_err)}")
        # ========== 确认结束 ==========
        
        task.status = "failed"
        task.error_message = str(e)
        db.commit()
        
        # 如果是套餐用户，恢复套餐次数
        sub = get_active_subscription(db, phone)
        if sub and cost == 0:
            sub.image_used = max(0, sub.image_used - num_images)
            db.commit()
            logger.info(f"任务 {task_id}: 已恢复套餐图片次数 {num_images} 张")
        else:
            # 非套餐用户，退款点数
            user = get_user_by_phone(db, phone)
            if user:
                user.credits += cost
                db.commit()
                logger.info(f"任务 {task_id}: 已退款 {cost} 点给 {phone}")
    finally:
        db.close()

# ========== 查看上传的图片 ==========
@app.get("/uploads/{filename}")
def get_uploaded_image(filename: str):
    """查看用户上传的图片"""
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        raise HTTPException(404, "图片不存在")

# ========== 通用下载代理 ==========
@app.get("/download")
def download_proxy(url: str):
    """代理下载，确保移动端也能下载"""
    try:
        resp = requests.get(url, timeout=60, stream=True)
        if resp.status_code != 200:
            raise HTTPException(404, "文件不存在")
        
        # 根据 URL 猜测文件类型
        if ".mp4" in url:
            media_type = "video/mp4"
            ext = "mp4"
        elif ".png" in url:
            media_type = "image/png"
            ext = "png"
        else:
            media_type = "image/jpeg"
            ext = "jpg"
        
        filename = f"video_{int(time.time())}.{ext}"
        
        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            resp.iter_content(chunk_size=1024*1024),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        raise HTTPException(500, f"下载失败: {str(e)}")

# ========== 支付宝充值接口 ==========
@app.post("/alipay/create")
async def create_alipay_payment(
    phone: str = Form(...),
    amount: float = Form(...),
    db: Session = Depends(get_db)
):
    """创建支付宝支付订单"""
    logger.info(f"支付宝充值请求: 手机号={phone}, 金额={amount}")
    
    user = get_user_by_phone(db, phone)
    if not user:
        raise HTTPException(404, "用户不存在")
    
    # 生成订单号
    order_id = f"RECHARGE_{phone}_{int(time.time())}"
    
    # 创建支付链接
    pay_url = create_wap_payment(
        order_id=order_id,
        amount=amount,
        subject=f"视频生成服务充值{amount}元"
    )
    
    logger.info(f"支付宝订单创建: {order_id}, 金额={amount}")
    
    return {
        "code": 200,
        "pay_url": pay_url,
        "order_id": order_id
    }

# ========== 支付宝异步通知 ==========
@app.post("/alipay/notify")
async def alipay_notify(request: Request, db: Session = Depends(get_db)):
    """支付宝异步通知"""
    logger.info("📢 收到支付宝回调")
    
    data = await request.form()
    notify_data = dict(data)
    
    logger.info(f"回调数据: {notify_data}")
    
    # 获取关键信息
    out_trade_no = notify_data.get("out_trade_no")
    trade_status = notify_data.get("trade_status")
    total_amount = notify_data.get("total_amount")
    
    logger.info(f"订单号: {out_trade_no}, 交易状态: {trade_status}, 金额: {total_amount}")
    
    if not out_trade_no:
        logger.error("回调缺少订单号")
        return "fail"
    
    if trade_status not in ["TRADE_SUCCESS", "TRADE_FINISHED"]:
        logger.info(f"交易状态非成功: {trade_status}")
        return "fail"
    
    # ========== 处理套餐订单 ==========
    if out_trade_no.startswith("SUB_"):
        parts = out_trade_no.split("_")
        # 格式: SUB_手机号_plan_500_a_时间戳
        if len(parts) >= 5:
            sub_phone = parts[1]
            plan = parts[2] + "_" + parts[3] + "_" + parts[4]
            logger.info(f"套餐订单解析: 手机号={sub_phone}, 套餐={plan}")
            
            if plan in SUBSCRIPTION_PLANS:
                plan_info = SUBSCRIPTION_PLANS[plan]
                sub = get_active_subscription(db, sub_phone)
                
                if sub:
                    # 续期
                    sub.plan_name = plan
                    sub.video_silent_limit = plan_info["video_silent"]
                    sub.video_audio_limit = plan_info["video_audio"]
                    sub.image_limit = plan_info["images"]
                    sub.video_silent_used = 0
                    sub.video_audio_used = 0
                    sub.image_used = 0
                    sub.end_date = datetime.utcnow() + timedelta(days=30)
                else:
                    sub = Subscription(
                        phone=sub_phone,
                        plan_name=plan,
                        video_silent_limit=plan_info["video_silent"],
                        video_audio_limit=plan_info["video_audio"],
                        image_limit=plan_info["images"],
                        start_date=datetime.utcnow(),
                        end_date=datetime.utcnow() + timedelta(days=30)
                    )
                    db.add(sub)
                
                db.commit()
                logger.info(f"✅ 套餐开通成功: {sub_phone}, 套餐: {plan}")
                return "success"
        
        return "fail"
    
    # ========== 处理点数充值 ==========
    # 解析手机号
    # 订单号格式: RECHARGE_15920978058_1788832801
    parts = out_trade_no.split("_")
    if len(parts) < 3:
        logger.error(f"订单号格式错误: {out_trade_no}")
        return "fail"
    
    phone = parts[1]
    logger.info(f"解析手机号: {phone}")
    
    # 充值
    try:
        amount = float(total_amount)
        # 精确点数映射
        credits_map = {
            9.9: 100,
            29.9: 300,
            49.9: 500
        }
        credits_to_add = credits_map.get(amount, int(amount * 10))
        
        user = get_user_by_phone(db, phone)
        if not user:
            logger.error(f"用户不存在: {phone}")
            return "fail"
        
        old_credits = user.credits
        user.credits += credits_to_add
        db.commit()
        
        logger.info(f"✅ 充值成功: {phone}, {old_credits} -> {user.credits} (增加{credits_to_add}点)")
        
        return "success"
        
    except Exception as e:
        logger.error(f"充值失败: {str(e)}")
        return "fail"

# ========== 查询订单状态 ==========
@app.get("/alipay/query/{order_id}")
async def query_alipay_order(order_id: str, db: Session = Depends(get_db)):
    """查询支付宝订单状态"""
    from alipay_pay import get_alipay_client
    alipay = get_alipay_client()
    result = alipay.api_alipay_trade_query(out_trade_no=order_id)
    
    trade_status = result.get("trade_status")
    
    # 如果支付成功且未充值
    if trade_status in ["TRADE_SUCCESS", "TRADE_FINISHED"]:
        phone = order_id.replace("RECHARGE_", "").rsplit("_", 1)[0]
        user = get_user_by_phone(db, phone)
        if user:
            amount = float(result.get("total_amount"))
            credits_map = {
                9.9: 100,
                29.9: 300,
                49.9: 500
            }
            credits_to_add = credits_map.get(amount, int(amount * 10))
            user.credits += credits_to_add
            db.commit()
            return {
                "code": 200,
                "status": "paid",
                "credits": user.credits
            }
    
    return {
        "code": 200,
        "status": trade_status
    }

# ========== 管理员开通套餐接口 ==========
@app.post("/admin/add_subscription")
def admin_add_subscription(
    phone: str = Form(...),
    plan: str = Form(...),
    admin_key: str = Form(...),
    db: Session = Depends(get_db)
):
    """管理员手动开通套餐"""
    if admin_key != config.ADMIN_KEY:
        raise HTTPException(403, "管理员密钥错误")
    
    if plan not in SUBSCRIPTION_PLANS:
        raise HTTPException(400, "无效的套餐")
    
    plan_info = SUBSCRIPTION_PLANS[plan]
    sub = get_active_subscription(db, phone)
    
    if sub:
        sub.plan_name = plan
        sub.video_silent_limit = plan_info["video_silent"]
        sub.video_audio_limit = plan_info["video_audio"]
        sub.image_limit = plan_info["images"]
        sub.video_silent_used = 0
        sub.video_audio_used = 0
        sub.image_used = 0
        sub.end_date = datetime.utcnow() + timedelta(days=30)
    else:
        sub = Subscription(
            phone=phone,
            plan_name=plan,
            video_silent_limit=plan_info["video_silent"],
            video_audio_limit=plan_info["video_audio"],
            image_limit=plan_info["images"],
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=30)
        )
        db.add(sub)
    
    db.commit()
    logger.info(f"✅ 管理员开通套餐: {phone}, 套餐: {plan}")
    return {"code": 200, "message": "套餐开通成功"}


# ========== 查询套餐信息 ==========
@app.get("/subscription/{phone}")
def get_subscription_info(phone: str, db: Session = Depends(get_db)):
    """查询用户套餐信息"""
    logger.info(f"查询套餐信息: {phone}")
    
    sub = get_active_subscription(db, phone)
    if not sub:
        return {"code": 200, "active": False}
    
    return {
        "code": 200,
        "active": True,
        "plan_name": sub.plan_name,
        "video_silent_remaining": sub.video_silent_limit - sub.video_silent_used,
        "video_audio_remaining": sub.video_audio_limit - sub.video_audio_used,
        "image_remaining": sub.image_limit - sub.image_used,
        "end_date": str(sub.end_date)
    }

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