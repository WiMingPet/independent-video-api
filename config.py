import os

# 可灵配置
KLING_API_KEY = os.getenv("KLING_API_KEY", "你的可灵Key")
KLING_API_URL = "https://api-beijing.klingai.com/v1"

# 管理员密钥（充值用）
ADMIN_KEY = os.getenv("ADMIN_KEY", "独立后端默认密钥")

# 数据库
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./video_users.db")

# 视频扣费
VIDEO_COSTS = {5: 50, 10: 100, 15: 150}