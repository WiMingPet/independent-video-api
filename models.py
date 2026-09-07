from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, unique=True, index=True)
    password = Column(String)
    credits = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())

class History(Base):
    __tablename__ = "history"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True)
    video_url = Column(String)
    type = Column(String, default="video")  # video 或 image
    created_at = Column(DateTime, default=func.now())

class VideoTask(Base):
    __tablename__ = "video_tasks"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True)
    task_id = Column(String, unique=True, index=True)
    status = Column(String, default="pending")  # pending, processing, completed, failed
    prompt = Column(Text, nullable=True)
    duration = Column(Integer, default=5)
    video_url = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    cost = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())