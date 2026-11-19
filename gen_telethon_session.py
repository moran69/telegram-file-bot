import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

try:
    api_id = int(os.getenv("API_ID") or 0)
except ValueError:
    raise SystemExit("请在 .env 中设置有效的正整数 API_ID") from None
api_hash = (os.getenv("API_HASH") or "").strip()
if api_id <= 0 or not api_hash:
    raise SystemExit("生成 session 需要在 .env 中设置 API_ID 和 API_HASH")

print("正在生成 Telethon 用户账号 StringSession。")
print("请按提示输入手机号、验证码；如果开启二步验证，还需要输入密码。\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n复制下面这一行到本机 .env：\n")
    print("USER_SESSION_STRING=" + client.session.save())
