import os

# 可灵配置
KLING_API_KEY = os.getenv("KLING_API_KEY", "你的可灵Key")
KLING_API_URL = "https://api-beijing.klingai.com/v1"

# 管理员密钥（充值用）
ADMIN_KEY = os.getenv("ADMIN_KEY", "独立后端默认密钥")

# 数据库
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./video_users.db")

# 视频扣费（无声）
VIDEO_COSTS = {5: 50, 10: 100, 15: 150}

# 视频扣费（有声）
VIDEO_COSTS_AUDIO = {5: 70, 10: 130, 15: 160}

# 支付宝配置
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "你的APP_ID")

# 从文件读取密钥
def read_key_file(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return f.read()
    return ""

ALIPAY_APP_PRIVATE_KEY = read_key_file("/data/alipay_private_key.txt")
ALIPAY_PUBLIC_KEY = read_key_file("/data/alipay_public_key.txt")

ALIPAY_NOTIFY_URL = "https://video-api.lingjing-media.com/alipay/notify"
ALIPAY_RETURN_URL = "https://media.lingjing-media.com/standalone-video.html"