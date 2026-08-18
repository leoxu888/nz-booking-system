"""认证工具：密码哈希（bcrypt）+ JWT（JSON Web Token）。

- 密码永不明文存储，统一用 bcrypt 加盐哈希。
- 每个登录请求返回一个 JWT，后续请求在 Authorization: Bearer <token> 里带上，
  后端解码出 {user_id, shop_id, role}，据此强制数据隔离。
"""
import os
import secrets
import hmac
import bcrypt
import jwt
from datetime import datetime, timezone

# JWT 签名密钥：
# - 生产环境务必在 .env 里设置强随机值（如 `python -c "import secrets;print(secrets.token_hex(32))"`）。
# - 若未设置，不再回退到「公开硬编码字符串」（那会被任何人伪造 super_admin 令牌、接管整站），
#   而是每次启动生成一个随机临时密钥。代价是重启后旧令牌失效（对本地开发可接受），
#   但绝不会给攻击者一个可预测的常量密钥。
_raw = os.getenv("SECRET_KEY")
if _raw:
    SECRET_KEY = _raw
else:
    SECRET_KEY = secrets.token_hex(32)
    print(
        "[SECURITY] SECRET_KEY 未设置 —— 已使用本次启动的随机临时密钥。"
        "生产部署请在 .env 中设置固定强随机 SECRET_KEY，否则每次重启会话都会失效。"
    )
ALGO = "HS256"
TOKEN_TTL_HOURS = 168  # 7 天


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_token(payload: dict, expires_hours: int = TOKEN_TTL_HOURS) -> str:
    """签发 JWT。调用方必须把以下字段传入 payload：
    - sub: 用户 id（int，自动转 str）
    - shop_id: 店铺 id（商家）或 None（超管）
    - role: 'shop_owner' | 'super_admin'
    - token_version: 用户当前 token_version（商家必带；超管可省，
      鉴权层对超管跳过版本校验）
    版本号不一致的旧 Token 会在鉴权时被拒绝（强制失效机制）。
    """
    exp = datetime.now(timezone.utc).timestamp() + expires_hours * 3600
    payload = {**payload, "exp": exp}
    # PyJWT 要求 sub(主题) 必须是字符串，统一转 str
    if "sub" in payload and payload["sub"] is not None:
        payload["sub"] = str(payload["sub"])
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGO)


def decode_token(token: str):
    """解码 JWT；失败（过期/伪造）返回 None。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGO])
    except Exception:
        return None


def secure_compare(a: str, b: str) -> bool:
    """恒定时间比较，避免计时侧信道（用于密码/令牌比对）。"""
    return hmac.compare_digest(a or "", b or "")
