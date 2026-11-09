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
HEARTBEAT_INTERVAL = 60   # 与 Telegram 的连接探测间隔
HEARTBEAT_FAILS = 3       # 连续失败这么多次就认为连接已无法恢复
HEARTBEAT_TIMEOUT = 30    # 单次探测的超时
STOP_TIMEOUT = 8

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


async def watch_connection(client, broken: asyncio.Event):
    """定期探测与 Telegram 的连接。会话失效（如 AuthKeyNotFound）后 Pyrogram 不会自行恢复，Bot 会假死不再响应，
    所以连续探测失败时通知主流程退出，由容器的重启策略重新登录"""
    fails = 0
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await asyncio.wait_for(client.get_me(), HEARTBEAT_TIMEOUT)
            fails = 0
        except Exception as e:
            fails += 1
            logger.warning(f"Telegram heartbeat failed ({fails}/{HEARTBEAT_FAILS}): {e!r}")
            if fails >= HEARTBEAT_FAILS:
                broken.set()
                return


def drop_session(client):
    """删除保存的会话文件，下次启动时用 Bot Token 重新登录（失效的会话不删的话，重启后仍然是坏的）"""
    database = client.storage.database
    if isinstance(database, str):    # 内存会话
        return
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            os.remove(f"{database}{suffix}")
        except FileNotFoundError:
            pass


async def stop_client(client):
    """停止客户端。连接已坏时 stop() 会长时间重试甚至抛错，这里限时并忽略异常"""
    try:
        await asyncio.wait_for(client.stop(), STOP_TIMEOUT)
    except Exception as e:
        logger.warning(f"Client stop failed: {e!r}")


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
        try:
            await app.start()
        except Exception:
            drop_session(app)
            raise
        logger.info('Bot Start')
        chat.me = await app.get_me()
        await handlers.refresh(app)
        await notify_su(app, "`Bot` 启动啦 ~" + version_notes() + (f"\n\n{note}" if (note := handlers.privacy_note()) else ""))
        broken = asyncio.Event()
        watchers = [asyncio.create_task(watch_config(app)), asyncio.create_task(watch_connection(app, broken))]
        waiting = [asyncio.create_task(pyrogram.idle()), asyncio.create_task(broken.wait())]   # 收到停止信号，或连接失效
        try:
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in watchers + waiting:
                task.cancel()
            await stop_client(app)
        if broken.is_set():
            logger.error("与 Telegram 的连接已失效且无法恢复，删除会话文件并退出，等待重新启动")
            drop_session(app)
            sys.exit(1)
    finally:
        await db.close()


def run():
    asyncio.run(main())
