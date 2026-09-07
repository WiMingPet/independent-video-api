import asyncio
import base64
import time
import logging
import requests
from datetime import datetime
import config
from database import SessionLocal
from models import User, History, VideoTask

logger = logging.getLogger(__name__)

async def process_video_task(task_id: str, phone: str, image_data: bytes, prompt: str, duration: int):
    """后台处理视频生成任务"""
    db = SessionLocal()
    task = None
    
    try:
        # 获取任务
        task = db.query(VideoTask).filter(VideoTask.task_id == task_id).first()
        if not task:
            logger.error(f"任务不存在: {task_id}")
            return
        
        # 获取用户
        user = db.query(User).filter(User.phone == phone).first()
        if not user:
            raise Exception("用户不存在")
        
        # 更新任务状态为处理中
        task.status = "processing"
        task.updated_at = datetime.now()
        db.commit()
        logger.info(f"任务 {task_id}: 开始处理")
        
        # 图片转base64
        image_b64 = base64.b64encode(image_data).decode()
        
        headers = {
            "Authorization": f"Bearer {config.KLING_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # 第一步：图生图修改内容
        logger.info(f"任务 {task_id}: 开始图片处理")
        edit_payload = {
            "model_name": "kling-v3",
            "prompt": prompt if prompt else "保持原图不变",
            "image": f"data:image/jpeg;base64,{image_b64}",
            "aspect_ratio": "1:1",
            "n": 1
        }
        
        resp = requests.post(
            f"{config.KLING_API_URL}/images/generations", 
            json=edit_payload, 
            headers=headers,
            timeout=30
        )
        edit_result = resp.json()
        
        if edit_result.get("code") != 0:
            raise Exception(f"图片处理失败: {edit_result.get('message')}")
        
        edit_task_id = edit_result["data"]["task_id"]
        edited_image_url = None
        
        # 轮询图片处理结果
        for i in range(30):
            await asyncio.sleep(5)
            try:
                edit_status_resp = requests.get(
                    f"{config.KLING_API_URL}/images/generations/{edit_task_id}",
                    headers=headers,
                    timeout=30
                )
                edit_status = edit_status_resp.json()["data"]
                
                logger.info(f"任务 {task_id}: 图片处理状态 {i+1}/30: {edit_status['task_status']}")
                
                if edit_status["task_status"] == "succeed":
                    edited_image_url = edit_status["task_result"]["images"][0]["url"]
                    logger.info(f"任务 {task_id}: 图片处理成功")
                    break
                elif edit_status["task_status"] == "failed":
                    raise Exception(edit_status.get("task_status_msg"))
            except Exception as e:
                logger.error(f"任务 {task_id}: 图片处理轮询异常: {str(e)}")
                if i >= 29:  # 最后一次尝试
                    raise
        
        if not edited_image_url:
            raise Exception("图片处理超时")
        
        # 第二步：图生视频
        logger.info(f"任务 {task_id}: 开始视频生成")
        video_payload = {
            "model_name": "kling-v2-6",
            "prompt": prompt if prompt else "让图片动起来",
            "duration": str(duration),
            "mode": "std",
            "with_audio": True,
            "image": edited_image_url
        }
        
        resp2 = requests.post(
            f"{config.KLING_API_URL}/videos/image2video", 
            json=video_payload, 
            headers=headers,
            timeout=30
        )
        video_result = resp2.json()
        
        if video_result.get("code") != 0:
            raise Exception(f"视频生成失败: {video_result.get('message')}")
        
        video_task_id = video_result["data"]["task_id"]
        
        # 轮询视频生成结果
        for i in range(60):
            await asyncio.sleep(5)
            try:
                video_status_resp = requests.get(
                    f"{config.KLING_API_URL}/videos/image2video/{video_task_id}",
                    headers=headers,
                    timeout=30
                )
                video_status = video_status_resp.json()["data"]
                
                logger.info(f"任务 {task_id}: 视频生成状态 {i+1}/60: {video_status['task_status']}")
                
                if video_status["task_status"] == "succeed":
                    video_url = video_status["task_result"]["videos"][0]["url"]
                    
                    # 扣费
                    user.credits -= task.cost
                    
                    # 保存到历史记录
                    history = History(phone=phone, video_url=video_url)
                    db.add(history)
                    
                    # 更新任务状态
                    task.video_url = video_url
                    task.status = "completed"
                    task.updated_at = datetime.now()
                    
                    db.commit()
                    logger.info(f"任务 {task_id}: 视频生成成功，URL={video_url}")
                    return
                    
                elif video_status["task_status"] == "failed":
                    raise Exception(video_status.get("task_status_msg"))
            except Exception as e:
                logger.error(f"任务 {task_id}: 视频生成轮询异常: {str(e)}")
                if i >= 59:  # 最后一次尝试
                    raise
        
        raise Exception("视频生成超时")
        
    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {str(e)}")
        
        # 更新任务状态为失败
        if task:
            task.status = "failed"
            task.error_message = str(e)
            task.updated_at = datetime.now()
            
            # 退款
            try:
                user = db.query(User).filter(User.phone == phone).first()
                if user:
                    user.credits += task.cost
                    logger.info(f"任务 {task_id}: 已退款 {task.cost} 点给 {phone}")
            except Exception as refund_error:
                logger.error(f"任务 {task_id}: 退款失败: {str(refund_error)}")
            
            db.commit()
        
    finally:
        db.close()