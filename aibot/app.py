# -*- coding: UTF-8 -*-
"""启动流程、版本更新通知与配置热重载"""

import os
import sys
import asyncio
import logging
import pyrogram
from loguru import logger
from . import VERSION, VERSION_ID, CHANGELOG
from .config import config, ConfigError
from . import db, sandbox, chat, handlers

logging.getLogger("pyrogram").setLevel(logging.ERROR)

WATCH_INTERVAL = 3

TEMPLATE = """# 最简配置，完整说明见 readme.config.yaml
SuAdmin:
- 123456789          # 你的 Telegram 用户 id
Bot:
  ApiId: 0           # https://my.telegram.org/apps
  ApiHash: ''
  Token: ''          # @BotFather
AI:
  Vendor:
    MyVendor:
      BaseUrl: https://example.com/v1
      Key: sk-xxxxxxxx
      Model:
      - gpt-5.5
  DefaultModel: gpt-5.5
"""


async def notify_su(client, text: str):
    for uid in set(config['SuAdmin']):
        try:
            await client.send_message(uid, text)
        except Exception as e:
            logger.warning(f"Notify {uid} failed: {e}")


def version_notes() -> str:
    """比 Other.CurrentVersion 新的更新介绍；全新安装只展示最新一条"""
    current = config['Other']['CurrentVersion']
    if current >= VERSION_ID:
        return ''
    notes = [(vid, text) for vid, text in sorted(CHANGELOG.items()) if vid > current]
    if current == 0:
        notes = notes[-1:]
    config['Other']['CurrentVersion'] = VERSION_ID
    config.save()
    return f"\n\n🎉 已更新到 `{VERSION} ({VERSION_ID})` 啦 ~\n\n" + "\n\n".join(f"**{vid}**\n{text}" for vid, text in notes)


async def watch_config(client):
    """config.yaml 被外部修改后自动重载；Bot 自身保存不会触发"""
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        if not config.changed_on_disk():
            continue
        try:
            config.load()
        except ConfigError as e:
            config.mtime = config.disk_mtime()
            logger.error(f"Config hot reload failed: {e}")
            await notify_su(client, f"❌ 配置热重载失败，继续使用旧配置:\n`{e}`")
            continue
        logger.info("Config hot reloaded")
        await sandbox.probe()
        await handlers.refresh(client)


def setup_logger():
    if config['Other']['Log']['Enable']:
        logger.add(config['Other']['Log']['Name'], rotation="10 MB", retention=5)


async def main():
    if not os.path.exists(config.path):
        with open(config.path, 'w', encoding='UTF-8') as fp:
            fp.write(TEMPLATE)
        logger.error(f"未找到配置文件，已生成模板 {os.path.abspath(config.path)}，请填写后重新启动")
        sys.exit(1)
    try:
        config.load()
    except ConfigError as e:
        logger.error(e)
        sys.exit(1)
    setup_logger()
    bot = config['Bot']
    missing = [k for k in ('SuAdmin',) if not config[k]] + [f"Bot.{k}" for k in ('ApiId', 'ApiHash', 'Token') if not bot[k]]
    if missing:
        logger.error(f"请先在 {os.path.abspath(config.path)} 中填写: {', '.join(missing)}")
        sys.exit(1)
    app = pyrogram.Client("AIBot", api_id=bot['ApiId'], api_hash=bot['ApiHash'], bot_token=bot['Token'], in_memory=not bot['CreateSession'],
                          proxy=bot['Proxy'], sleep_threshold=bot['SleepThreshold'], max_concurrent_transmissions=8)
    for handler, group in handlers.HANDLERS:
        app.add_handler(handler, group)
    await db.init()
    try:
        await sandbox.probe()
        await app.start()
        logger.info('Bot Start')
        chat.me = await app.get_me()
        await handlers.refresh(app)
        await notify_su(app, "`Bot` 启动啦 ~" + version_notes())
        watcher = asyncio.create_task(watch_config(app))
        try:
            await pyrogram.idle()
        finally:
            watcher.cancel()
            await app.stop()
    finally:
        await db.close()


def run():
    asyncio.run(main())
