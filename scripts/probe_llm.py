"""探测脚本：找出哪套模型配置真正可用（纯文本 + 图片两轮探测）。

用法（在服务器上，项目根目录）：
    .venv/bin/python scripts/probe_llm.py

依次探测：
1. 服务端 .env 的 CHAT_* 配置
2. 数据库里每个用户 ⚙️ 保存的个人模型配置（脱敏展示）

探测结果 ✓ = 该配置可用；✗ 后面的错误信息就是平台返回的原文。
找到能同时通过「文本+图片」的配置后，把它填进 .env 的 OCR_BASE_URL/OCR_KEY/OCR_MODEL
（面试/OCR 走服务端配置），重启后端即可。
"""
# -*- coding: utf-8 -*-
import io
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台 GBK 打不了 ✓/✗，统一 UTF-8 输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from core.config import settings  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from DAO.user_dao import UserDAO  # noqa: E402
from model.UserModel import UserModel  # noqa: E402

# 1x1 透明 PNG（测图片通道用）
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def mask(key: str) -> str:
    return (key[:6] + "***" + key[-4:]) if key and len(key) > 12 else "***"


def probe(base_url: str, key: str, model: str, with_image: bool):
    url = base_url.rstrip("/").removesuffix("/chat/completions") + "/chat/completions"
    content = (
        [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
            {"type": "text", "text": "图里有什么？一个字回答"},
        ]
        if with_image
        else "只回复：好"
    )
    body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 64}
    try:
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=40)
        if r.status_code == 200:
            return True, r.json()["choices"][0]["message"]["content"][:30]
        return False, f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def report(tag: str, base_url: str, key: str, model: str) -> bool:
    """文本+图片两轮探测，打印结果；图片通过才算全通"""
    ok_t, msg_t = probe(base_url, key, model, False)
    ok_i, msg_i = probe(base_url, key, model, True)
    print(f"[{tag}] {model} @ {urlparse(base_url).netloc or base_url[:30]} key={mask(key)}")
    print(f"    文本: {'✓ ' + msg_t if ok_t else '✗ ' + msg_t}")
    print(f"    图片: {'✓ ' + msg_i if ok_i else '✗ ' + msg_i}")
    return ok_t and ok_i


def main():
    env_model = settings.CHAT_MODEL or "qwen3.8-max"
    print("===== 1) 服务端 .env 配置 =====")
    if settings.CHAT_BASE_URL and settings.CHAT_KEY:
        report(".env", settings.CHAT_BASE_URL, settings.CHAT_KEY, env_model)
    else:
        print("    .env 未配置 CHAT_BASE_URL/CHAT_KEY")

    print("\n===== 2) 用户 ⚙️ 个人配置 =====")
    db = SessionLocal()
    seen = set()
    found = False
    for u in db.query(UserModel).all():
        cfg = UserDAO(db).get_llm_config(u.id)
        if not (cfg["base_url"].strip() and cfg["api_key"].strip()):
            print(f"  {u.email}: 未配置")
            continue
        k = (cfg["base_url"], cfg["api_key"], cfg["model"])
        if k in seen:
            continue
        seen.add(k)
        ok = report(u.email, cfg["base_url"], cfg["api_key"], cfg["model"].strip() or env_model)
        found = found or ok
    db.close()

    print()
    if found:
        print("→ 有 ✓ 的配置：把它的 地址/密钥/模型 填进 .env 的 OCR_BASE_URL / OCR_KEY / OCR_MODEL，"
              "重启后端，模拟面试即可使用")
    else:
        print("→ 全部配置都失败：到百炼控制台确认模型开通状态与 API Key 归属（主账号/工作区）")


if __name__ == "__main__":
    main()
