from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# 检查是否有持久化目录
if os.path.exists('/data'):
    # 使用 Zeabur 持久化存储
    DATABASE_URL = "sqlite:////data/video_service.db"
    print("使用持久化存储: /data/video_service.db")
else:
    # 本地开发环境
    DATABASE_URL = "sqlite:///./video_service.db"
    print("使用本地存储: ./video_service.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()