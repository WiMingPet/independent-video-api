from alipay import AliPay
import config

def get_alipay_client():
    """获取支付宝客户端"""
    alipay = AliPay(
        appid=config.ALIPAY_APP_ID,
        app_notify_url=config.ALIPAY_NOTIFY_URL,
        app_private_key_string=config.ALIPAY_APP_PRIVATE_KEY,
        alipay_public_key_string=config.ALIPAY_PUBLIC_KEY,
        sign_type="RSA2",
        debug=False
    )
    return alipay

def create_page_payment(order_id, amount, subject):
    """创建电脑网站支付"""
    alipay = get_alipay_client()
    
    order_string = alipay.api_alipay_trade_page_pay(
        out_trade_no=order_id,
        total_amount=str(amount),
        subject=subject,
        return_url=config.ALIPAY_RETURN_URL,
        notify_url=config.ALIPAY_NOTIFY_URL
    )
    
    return f"https://openapi.alipay.com/gateway.do?{order_string}"

def create_wap_payment(order_id, amount, subject):
    """创建手机网站支付"""
    alipay = get_alipay_client()
    
    order_string = alipay.api_alipay_trade_wap_pay(
        out_trade_no=order_id,
        total_amount=str(amount),
        subject=subject,
        return_url=config.ALIPAY_RETURN_URL,
        notify_url=config.ALIPAY_NOTIFY_URL
    )
    
    return f"https://openapi.alipay.com/gateway.do?{order_string}"

def verify_notification(data, signature):
    """验证异步通知签名"""
    alipay = get_alipay_client()
    return alipay.verify(data, signature)