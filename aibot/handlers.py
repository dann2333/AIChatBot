# -*- coding: UTF-8 -*-
"""Telegram 指令、按钮回调、自动回复、访客与内联入口"""

import os
import re
import time
import uuid
import asyncio
import datetime
import functools
import pyrogram
from pyrogram import filters
from pyrogram.handlers import (MessageHandler, CallbackQueryHandler, InlineQueryHandler, ChosenInlineResultHandler,
                               GuestMessageHandler, StoppedMessageGenerationHandler)
from pyrogram.types import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle,
                            InputTextMessageContent, ReplyParameters)
from loguru import logger
from . import VERSION, VERSION_ID, VERSION_SOURCE
from .config import config, user_conf, set_user, is_admin, is_su, can_chat, model_map, USER_DEFAULT
from .chat import spawn
from . import chat, db, llm, sandbox, panel

HANDLERS = []
EFFORTS = {"auto": "默认", "none": "关闭", "minimal": "最小", "low": "低", "medium": "中", "high": "高", "xhigh": "极高"}
DENY = {
    'chat': "❌ 您没有权限或本群并未加入白名单呢 请联系超管授权 ~",
    'admin': "❌ 您没有权限呢 请联系超管授权 ~",
    'su': "❌ 您没有权限呢 请在配置文件中设置呢 ~",
}
COMMANDS = [("help", "获取帮助菜单"), ("version", "获取版本信息"), ("stats", "获取权限状态"), ("chat", "发送聊天内容"),
            ("clear", "清除上下文记录"), ("memory", "查看或清除长期记忆"), ("model", "修改聊天模型"), ("prune", "测试模型可用性"),
            ("effort", "设置思维链深度"), ("prompt", "设置系统提示词"), ("context", "开关上下文记录")]


def on(handler_cls, flt=None, group=0):
    def deco(fn):
        HANDLERS.append((handler_cls(fn, flt) if flt is not None else handler_cls(fn), group))
        return fn
    return deco


def command(*names):
    return on(MessageHandler, filters.command(list(names)))


def require(level):
    """权限检查：chat=可对话 admin=管理 su=超管"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(client, message):
            if not message.from_user:
                return
            uid = message.from_user.id
            ok = {'chat': lambda: can_chat(uid, message.chat.id), 'admin': lambda: is_admin(uid), 'su': lambda: is_su(uid)}[level]()
            if not ok:
                return await reply(client, message, DENY[level])
            return await fn(client, message)
        return wrapper
    return deco


async def _delete_later(client, msgs, delay):
    await asyncio.sleep(delay)
    for m in msgs:
        if m:
            try:
                await client.delete_messages(m.chat.id, m.id)
            except Exception:
                pass


async def reply(client, message, text, delete=10, markup=None):
    msg = await client.send_message(message.chat.id, text, reply_parameters=ReplyParameters(message_id=message.id), reply_markup=markup)
    if delete:
        spawn(_delete_later(client, [msg, message], delete))
    return msg


def args(message) -> str:
    parts = (message.text or message.caption or '').split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ''


def effort_name(effort) -> str:
    return EFFORTS.get(effort or config['AI']['ReasoningEffort'], EFFORTS['auto'])


async def refresh(client):
    """配置变化后刷新命令面板与日志"""
    try:
        await client.set_bot_commands([] if config['Bot']['HideCommand'] else [BotCommand(c, d) for c, d in COMMANDS])
    except Exception as e:
        logger.warning(f"Set bot commands failed: {e}")


# ---------- 共用的文本与面板（普通消息与访客模式都会使用） ----------

async def stats_text(uid: int, chat_id: int, conv: str) -> str:
    mark = lambda ok: "✔" if ok else "❌"
    lines = ["你当前的权限状态是:\n", f"管理权限: {mark(is_admin(uid))}", f"超管权限: {mark(is_su(uid))}",
             f"可以对话: {mark(can_chat(uid, chat_id))}"]
    if is_su(uid):
        lines += ["", f"超管数量: `{len(set(config['SuAdmin']))}`", f"管理数量: `{len(set(config['Admin']) - set(config['SuAdmin']))}`",
                  f"模型数量: `{len(model_map())}`", f"群组数量: `{len(config['AI']['WhiteList'])}`"]
    if can_chat(uid, chat_id):
        settings, tool = user_conf(uid), config['AI']['Tool']
        lines += ["", f"当前对话数量: `{await db.count(conv)}`", f"长期记忆数量: `{len(await db.list_memories(f'user:{uid}'))}`",
                  f"上下文记录: {mark(settings['Context'])}",
                  f"当前模型: `{settings['Model'] or config['AI']['DefaultModel']}`", f"默认模型: `{config['AI']['DefaultModel']}`",
                  f"当前思维链深度: `{effort_name(settings['ReasoningEffort'])}`", f"默认思维链深度: `{effort_name(config['AI']['ReasoningEffort'])}`", "",
                  f"网络搜索: {mark(tool['Enable'] and tool['WebSearch']['Enable'])}", f"链接请求: {mark(tool['Enable'] and tool['FetchURL']['Enable'])}",
                  f"沙箱 Shell: {mark(sandbox.enabled())}" + (f" `{sandbox.reason}`" if sandbox.reason and tool['Shell']['Enable'] else ''),
                  f"长期记忆: {mark(tool['Enable'] and config['AI']['Memory']['Enable'])}", f"滚动摘要: {mark(config['AI']['Summary']['Enable'])}",
                  f"文件直传: {mark(config['AI']['FileSupport'])}"]
    return "\n".join(lines)


def model_panel(uid: int, page: int = 1, closable: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    names, per = list(model_map()), 20
    total = max(1, -(-len(names) // per))
    page = min(max(page, 1), total)
    start = (page - 1) * per
    buttons = [InlineKeyboardButton(n, callback_data=f"m {uid} s {start + i}") for i, n in enumerate(names[start:start + per])]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton('上一页', callback_data=f"m {uid} j {page - 1}") if page > 1 else InlineKeyboardButton('      ', callback_data='noop'),
                 InlineKeyboardButton(f'{page} / {total}', callback_data=f"pgi {page} {total}"),
                 InlineKeyboardButton('下一页', callback_data=f"m {uid} j {page + 1}") if page < total else InlineKeyboardButton('      ', callback_data='noop')])
    if closable:
        rows.append([InlineKeyboardButton("关闭", callback_data=f"m {uid} c")])
    current = user_conf(uid)['Model'] or config['AI']['DefaultModel']
    return f"🔗 当前模型为 `{current}` 当前有 `{len(names)}` 个模型 请选择要更换的模型:", InlineKeyboardMarkup(rows)


def effort_panel(uid: int, closable: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    b = lambda e: InlineKeyboardButton(EFFORTS[e], callback_data=f"r {uid} s {e}")
    rows = [[b('auto'), b('none')], [b('minimal'), b('xhigh')], [b('low'), b('medium'), b('high')]]
    if closable:
        rows.append([InlineKeyboardButton('关闭', callback_data=f"r {uid} c")])
    return f"🔗 当前思维链深度为 `{effort_name(user_conf(uid)['ReasoningEffort'])}` 请选择要修改的思维链深度:", InlineKeyboardMarkup(rows)


def switch_model(uid: int, name: str) -> str:
    names = list(model_map())
    if name not in names:
        return "❌ 您选择的模型不存在哦 ~\n\n当前可用模型有: \n\n`" + "`\n`".join(names) + "`"
    old = user_conf(uid)['Model'] or config['AI']['DefaultModel']
    set_user(uid, 'Model', name)
    return f"✔ 已将模型 `{old}` 更换为模型 `{name}` ~"


def id_text(message) -> str:
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    prefix = 'R' if target is not message.from_user else ''
    lines = [f"**Chat**: `{message.chat.id}`", f"**{prefix}ID**: `{target.id}`"]
    if target.language_code:
        lines.append(f"**{prefix}Locale**: `{target.language_code}`")
    if target.dc_id:
        lines.append(f"**{prefix}DC**: `{target.dc_id}`")
    return "\n".join(lines)


async def memory_text(uid: int, conv: str, arg: str) -> str:
    arg = arg.strip()
    if arg == 'clear':
        await db.clear_memories(f"user:{uid}")
        return "✔ 已清除你的长期记忆 ~"
    if arg == 'clear chat':
        if not is_admin(uid):
            return DENY['admin']
        await db.clear_memories(f"chat:{conv}")
        return "✔ 已清除本聊天的长期记忆 ~"
    mine, shared = await db.list_memories(f"user:{uid}"), await db.list_memories(f"chat:{conv}")
    fmt = lambda rows: "\n".join(f"- `{m['topic']}`: {m['content']}" for m in rows) or "(无)"
    return (f"🧠 你的长期记忆:\n{fmt(mine)}\n\n💬 本聊天的长期记忆:\n{fmt(shared)}\n\n"
            "发送 `/memory clear` 清除你的记忆，`/memory clear chat` 清除本聊天的记忆（管理员）")


async def trust_text(uid: int, chat_id: int, add: bool) -> str:
    whitelist = config['AI']['WhiteList']
    if add and chat_id in whitelist:
        return "❌ 此群组已被加入白名单了哦 ~"
    if not add and chat_id not in whitelist:
        return "❌ 此群组还未被加入白名单哦 ~"
    logger.info(f"SuAdmin {uid} {'Trust' if add else 'DisTrust'} {chat_id}")
    whitelist.append(chat_id) if add else whitelist.remove(chat_id)
    config.save()
    return f"✔ 已{'添加' if add else '删除'}白名单群组 `{chat_id}` ~"


# ---------- 配置面板输入（优先于其他处理器） ----------

@on(MessageHandler, filters.private | filters.group, group=-1)
async def panel_input(client, message):
    if panel.pending and await panel.on_input(client, message, refresh):
        message.stop_propagation()


# ---------- 公共指令 ----------

@command('help')
async def cmd_help(client, message):
    uid = message.from_user.id if message.from_user else 0
    lines = [f"欢迎使用 `{chat.me.first_name}` 呢 你可以使用以下指令呢 ~\n", "/help `获取帮助菜单`",
             f"/version `获取版本信息 - {VERSION} ({VERSION_ID})`", "/stats `获取权限状态`"]
    if can_chat(uid, message.chat.id):
        lines += ["/chat `发送聊天内容`", "/clear `清除上下文记录`", "/page `跳转页数`", "/memory `查看或清除长期记忆`"]
    if is_admin(uid):
        lines += ["/model `[管理]修改聊天模型`", "/prune `[管理]测试模型可用性`", "/effort `[管理]设置思维链深度`",
                  "/prompt `[管理]设置系统提示词`", "/context `[管理]开关上下文记录`"]
    if is_su(uid):
        lines += ["/trust `[超管]添加白名单群组`", "/distrust `[超管]删除白名单群组`", "/grant `[超管]授权一个用户`", "/ungrant `[超管]取消用户授权`",
                  "/grantscan `[超管]扫描清理授权`", "/get `[超管]读取配置`", "/set `[超管]修改配置`", "/config `[超管]配置面板`",
                  "/reload `[超管]重载配置文件`", "/id `[超管]查看 ID`", "/leave `[超管]退出群组`", "/stop `[超管]停止运行程序`"]
    if not config['Other']['HideAds']:
        lines.append(f"\nPowered by AIBot `{VERSION}`")
    await reply(client, message, "\n".join(lines), delete=0 if message.chat.type == pyrogram.enums.ChatType.PRIVATE else 10)


@command('version')
async def cmd_version(client, message):
    await reply(client, message, f"Version: `{VERSION} ({VERSION_ID})`\nSource: `{VERSION_SOURCE}`")


@command('stats')
async def cmd_stats(client, message):
    if message.from_user:
        await reply(client, message, await stats_text(message.from_user.id, message.chat.id, str(message.chat.id)))


@command('start')
async def cmd_start(client, message):
    if len((message.text or '').split()) == 1 and config['Other']['Intro']['Enable']:
        await reply(client, message, config['Other']['Intro']['Text'], delete=0)


# ---------- 对话指令 ----------

@command('chat', 'c')
@require('chat')
async def cmd_chat(client, message):
    text = args(message)
    if not text and not (message.photo or message.document or message.sticker or message.reply_to_message):
        return await reply(client, message, "❌ 命令格式不对哦 ~ 要这样使用呢: `/chat <内容>`")
    spawn(chat.from_message(client, message, text))


@command('clear')
@require('chat')
async def cmd_clear(client, message):
    await db.clear(str(message.chat.id))
    if message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        await db.clear(f"inline:{message.from_user.id}")
    await reply(client, message, "✔ 已清除当前聊天的记录呢 ~")


@command('page', 'p')
@require('chat')
async def cmd_page(client, message):
    try:
        page = int(args(message))
    except ValueError:
        return await reply(client, message, "❌ 命令格式不对哦 ~ 要这样使用呢: `/page <页数>`")
    target = message.reply_to_message
    key = chat.msg_views.get(f"{message.chat.id}:{target.id}") if target else None
    if not key or key not in chat.views:
        return await reply(client, message, "❌ 请回复包含翻页内容的消息呢 ~")
    await _delete_later(client, [message], 0)
    await show_page(client, key, page, chat_id=message.chat.id, message_id=target.id)


async def show_page(client, key, page, chat_id=None, message_id=None, callback=None):
    text = chat.views[key]
    total = chat.page_count(text)
    page = min(max(page, 1), total)
    body, markup = chat.rich(chat.page_of(text, page)), InlineKeyboardMarkup(chat.page_markup(key, page, total))
    if callback:
        await callback.edit_message_text(rich_message=body, reply_markup=markup)
    else:
        await client.edit_message_text(chat_id, message_id, rich_message=body, reply_markup=markup)


@command('memory')
@require('chat')
async def cmd_memory(client, message):
    await reply(client, message, await memory_text(message.from_user.id, str(message.chat.id), args(message)), delete=30)


# ---------- 管理指令 ----------

@command('context')
@require('admin')
async def cmd_context(client, message):
    uid = message.from_user.id
    enabled = not user_conf(uid)['Context']
    set_user(uid, 'Context', enabled)
    await reply(client, message, f"✔ 已{'开启' if enabled else '关闭'}上下文记录呢 ~")


@command('prompt')
@require('admin')
async def cmd_prompt(client, message):
    uid, prompt = message.from_user.id, args(message)
    old = user_conf(uid)['SystemPrompt']
    set_user(uid, 'SystemPrompt', prompt)
    await reply(client, message, f"✔ 已修改系统提示词 ~\n\n修改前: `{old}`\n修改后: `{prompt}`" if prompt else f"✔ 已删除系统提示词 ~\n\n修改前: `{old}`")


@command('reason', 'effort', 'reasoneffort')
@require('admin')
async def cmd_effort(client, message):
    text, markup = effort_panel(message.from_user.id)
    await reply(client, message, text, delete=0, markup=markup)


@command('model')
@require('admin')
async def cmd_model(client, message):
    uid = message.from_user.id
    if not model_map():
        return await reply(client, message, "❌ 您还没有配置任何模型哦 ~")
    if name := args(message).strip():
        return await reply(client, message, switch_model(uid, name))
    text, markup = model_panel(uid)
    await reply(client, message, text, delete=0, markup=markup)


prune_state = {'running': False, 'stop': False, 'history': None}


@command('prune')
@require('admin')
async def cmd_prune(client, message):
    if not is_su(message.from_user.id):
        history = prune_state['history']
        if history is None:
            return await reply(client, message, "❌ 当前没有模型可用性结果呢 ~")
        return await reply(client, message, f"✏️ **模型可用性测试** - `{history[0]}`\n{history[1]}", delete=0)
    if prune_state['running']:
        return await reply(client, message, "❌ 正在检测模型可用性哦 ~ 请过会再尝试吧 ~")
    keyword = args(message).strip()
    try:
        pattern = re.compile(keyword or '.', re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    names = [n for n in model_map() if pattern.search(n)]
    if not names:
        return await reply(client, message, "❌ 没有匹配的模型呢 ~")
    logger.info(f"SuAdmin {message.from_user.id} Prune {keyword}")
    spawn(run_prune(client, message, names))


async def run_prune(client, message, names):
    prune_state.update(running=True, stop=False)
    stop_btn = InlineKeyboardMarkup([[InlineKeyboardButton('停止', callback_data='prune')]])
    msg = await reply(client, message, "⏳ 测试模型可用性中 ...", delete=0, markup=stop_btn)
    sem, results, done = asyncio.Semaphore(max(config['Network']['Thread'], 1)), {}, 0
    step = max(config['Other']['RefreshFrequency'], 1)

    async def test(name):
        nonlocal done
        async with sem:
            if prune_state['stop']:
                return
            start = time.time()
            try:
                ok = bool(await llm.complete(name, [{"role": "user", "content": "hi"}], timeout=max(config['Network']['Timeout'], 30)))
            except Exception:
                ok = False
            results[name] = int((time.time() - start) * 1000) if ok else -1
            done += 1
            if int(done * 100 / len(names) / step) > int((done - 1) * 100 / len(names) / step) and done < len(names):
                bar = int(done * 20 / len(names))
                await chat._try(client.edit_message_text(msg.chat.id, msg.id, f"⏳ 测试模型可用性中 ...\n\n[`{'=' * bar}{' ' * (20 - bar)}`]\n\n剩余任务数量: `{len(names) - done}`", reply_markup=stop_btn))

    try:
        await asyncio.gather(*(test(n) for n in names))
        if prune_state['stop']:
            await chat._try(client.edit_message_text(msg.chat.id, msg.id, "✔ 任务已停止 ~"))
            return
        content = "".join(f"\n**{n}**: " + (f"✔ - `{results[n]}ms`" if results.get(n, -1) >= 0 else "❌") for n in names)
        prune_state['history'] = (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), content)
        await chat._try(client.edit_message_text(msg.chat.id, msg.id, "✏️ **模型可用性测试**\n" + content))
    finally:
        prune_state.update(running=False, stop=False)


# ---------- 超管指令 ----------

@command('trust')
@require('su')
async def cmd_trust(client, message):
    if message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        return await reply(client, message, "❌ 私聊无需加入白名单哦 ~")
    await reply(client, message, await trust_text(message.from_user.id, message.chat.id, True))


@command('distrust')
@require('su')
async def cmd_distrust(client, message):
    await reply(client, message, await trust_text(message.from_user.id, message.chat.id, False))


@command('reload')
@require('su')
async def cmd_reload(client, message):
    from .config import ConfigError
    try:
        config.load()
    except ConfigError as e:
        return await reply(client, message, f"❌ 配置重载失败: `{e}`", delete=30)
    await sandbox.probe()
    await refresh(client)
    await reply(client, message, "✔ 配置重载完毕啦 ~")


def _target_ids(message) -> list[int]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return [message.reply_to_message.from_user.id]
    ids = []
    for token in args(message).split():
        try:
            ids.append(int(token))
        except ValueError:
            pass
    return ids


@command('grant')
@require('su')
async def cmd_grant(client, message):
    ids = [i for i in _target_ids(message) if not is_admin(i)]
    if not ids:
        return await reply(client, message, "❌ 没有可授权的id呢 ~ 用法: 回复用户或 `/grant <id...>`")
    for i in ids:
        config['Admin'].append(i)
        config['User'].setdefault(i, dict(USER_DEFAULT))
    config.save()
    await reply(client, message, f"✔ 已将 {' '.join(f'`{i}`' for i in ids)} 授权为管理员 ~")


@command('ungrant')
@require('su')
async def cmd_ungrant(client, message):
    ids = [i for i in _target_ids(message) if i in config['Admin'] and not is_su(i)]
    if not ids:
        return await reply(client, message, "❌ 没有可取消授权的id呢 ~ 超管需要在配置文件中修改")
    config['Admin'][:] = [i for i in config['Admin'] if i not in ids]
    config.save()
    await reply(client, message, f"✔ 已取消 {' '.join(f'`{i}`' for i in ids)} 的授权 ~")


@command('grantscan')
@require('su')
async def cmd_grantscan(client, message):
    logger.info(f"SuAdmin {message.from_user.id} ScanGrant")
    msg = await reply(client, message, "✔ 扫描中 ~", delete=0)
    removed = []
    for uid in list(config['Admin']):
        try:
            if (await client.get_users(uid)).is_deleted:
                removed.append(uid)
        except pyrogram.errors.PeerIdInvalid:
            removed.append(uid)
        except Exception:
            pass
    config['Admin'][:] = [i for i in config['Admin'] if i not in removed]
    config.save()
    await client.edit_message_text(msg.chat.id, msg.id, f"✔ 扫描完毕 已清除 `{len(removed)}` 个授权 ~")
    spawn(_delete_later(client, [msg, message], 10))


@command('get')
@require('su')
async def cmd_get(client, message):
    await reply(client, message, panel.cmd_get(args(message)), delete=30)


@command('set')
@require('su')
async def cmd_set(client, message):
    parts = args(message).split(maxsplit=1)
    if len(parts) < 2:
        return await reply(client, message, "❌ 命令格式不对哦 ~ 要这样使用呢: `/set <配置路径> <值>`，空格请写作 `\\s`")
    ok, text = panel.cmd_set(parts[0], parts[1])
    if ok:
        await refresh(client)
    await reply(client, message, text, delete=30)


@command('config')
@require('su')
async def cmd_config(client, message):
    path = args(message).strip() or '.'
    try:
        config.get(path)
    except KeyError:
        return await reply(client, message, f"❌ 配置项 `{path}` 不存在哦 ~")
    token = panel.open_session(message.from_user.id, path)
    text, markup = panel.render(token)
    await reply(client, message, text, delete=0, markup=markup)


@command('id')
@require('su')
async def cmd_id(client, message):
    await reply(client, message, id_text(message))


@command('leave')
@require('su')
async def cmd_leave(client, message):
    logger.info(f"Leave Group {message.chat.id}")
    await client.leave_chat(message.chat.id)


@command('stop')
@require('su')
async def cmd_stop(client, message):
    if args(message).strip() != config['Other']['Password']:
        return await reply(client, message, "❌ 密码错误哦 ~")
    logger.info(f"SuAdmin {message.from_user.id} Stop Bot")
    await reply(client, message, "✔ 正在关闭 `Bot` ~", delete=0)
    await db.close()
    os._exit(0)


# ---------- 事件 ----------

@on(MessageHandler, filters.new_chat_members)
async def auto_leave(client, message):
    mode, uid = config['Other']['AvoidJoinGroups'], message.from_user.id if message.from_user else 0
    if not any(m.is_self for m in message.new_chat_members):
        return
    if (mode == 1 and not is_admin(uid)) or (mode == 2 and not is_su(uid)):
        await client.send_message(message.chat.id, f"❌ 机器人已启动防拉群模式 请联系{'管理员' if mode == 1 else '超管'}拉群")
        await client.leave_chat(message.chat.id)


@on(MessageHandler)
async def auto_reply(client, message):
    if not message.from_user or not config['AI']['AutoReply'] or not can_chat(message.from_user.id, message.chat.id):
        return
    text = chat.safe_text(message.text or message.caption)
    if not text and not (message.photo or message.document or message.sticker):
        return
    lowered = text.lower()
    for name in (chat.me.username, chat.me.first_name):
        if name and f"@{name}".lower() in lowered:
            return spawn(chat.from_message(client, message, remove_mention(text, f"@{name}")))
    replied = message.reply_to_message
    if message.chat.type == pyrogram.enums.ChatType.PRIVATE or (replied and replied.from_user and replied.from_user.is_self):
        return spawn(chat.from_message(client, message, text))
    for word in config['AI']['WakeWord']:
        try:
            if re.search(word, text, re.IGNORECASE):
                return spawn(chat.from_message(client, message, text))
        except re.error:
            continue


def remove_mention(text: str, mention: str) -> str:
    """去掉消息开头、结尾或中间单独出现的 @机器人"""
    pattern = re.compile(r'(^|\s)' + re.escape(mention) + r'(?=\s|$)', re.IGNORECASE)
    stripped = re.sub(r'\s{2,}', ' ', pattern.sub(' ', text)).strip()
    return stripped or text


@on(InlineQueryHandler)
async def inline_query(client, query):
    if not is_admin(query.from_user.id):
        return await query.answer([InlineQueryResultArticle('您没有权限使用这个命令呢', InputTextMessageContent("❌ 您没有权限呢 请联系超管授权 ~"), description='请联系超管授权')], cache_time=1)
    if not query.query.strip():
        return await query.answer([InlineQueryResultArticle('请输入内容呢', InputTextMessageContent(f"❌ 请按照 `@{chat.me.username} 内容` 的格式填写呢 ~"), description='请填写需要发送的内容')], cache_time=1)
    await query.answer([InlineQueryResultArticle('点我发送消息', InputTextMessageContent("⏳ 思考中 ..."), id=str(uuid.uuid4()), description=query.query,
                                                 reply_markup=chat.stop_markup('stop'))], cache_time=1, is_personal=True)


@on(ChosenInlineResultHandler)
async def chosen_inline(client, chosen):
    if is_admin(chosen.from_user.id) and chosen.inline_message_id:
        spawn(chat.from_inline(client, chosen))


@on(GuestMessageHandler)
async def guest_message(client, message):
    if not message.from_user:
        return
    conv, chat_id = chat.chat_conv(message)
    uid = message.from_user.id
    if not can_chat(uid, chat_id):
        return
    text = chat.safe_text(message.text or message.caption)
    if chat.me.username:
        text = remove_mention(text, f"@{chat.me.username}")
    cmd, _, rest = text.partition(' ')
    cmd = cmd.split('@')[0].lower()

    async def answer(body, markup=None):
        await client.answer_guest_query(message.guest_query_id, InlineQueryResultArticle(
            body.split('\n')[0][:64] or '...', InputTextMessageContent(body), reply_markup=markup))

    if cmd == '/id':
        return await answer(id_text(message))
    if cmd == '/clear':
        await db.clear(conv)
        return await answer("✔ 已清除当前聊天的记录呢 ~")
    if cmd == '/stats':
        return await answer(await stats_text(uid, chat_id, conv))
    if cmd == '/memory':
        return await answer(await memory_text(uid, conv, rest))
    if cmd in ('/reason', '/effort', '/reasoneffort', '/model') and not is_admin(uid):
        return await answer(DENY['admin'])
    if cmd in ('/reason', '/effort', '/reasoneffort'):
        return await answer(*effort_panel(uid, closable=False))
    if cmd == '/model':
        if rest.strip():
            return await answer(switch_model(uid, rest.strip()))
        return await answer(*model_panel(uid, closable=False))
    if cmd in ('/trust', '/distrust'):
        return await answer(await trust_text(uid, chat_id, cmd == '/trust') if is_su(uid) else DENY['su'])
    spawn(chat.from_guest(client, message, text))


@on(StoppedMessageGenerationHandler)
async def draft_stopped(client, update):
    gen = chat.generations.get(chat.draft_gens.get(update.draft_id))
    if gen:
        gen['stop'] = True


@on(CallbackQueryHandler)
async def callback(client, cq):
    parts = (cq.data or '').split()
    kind = parts[0] if parts else ''
    uid = cq.from_user.id
    if kind == 'noop':
        return await cq.answer()
    if kind == 'pgi':
        return await cq.answer(f"第 {parts[1]} 页  共 {parts[2]} 页", show_alert=True)
    if kind == 'pg':
        if parts[1] not in chat.views:
            return await cq.answer("❌ 内容已过期了呢 ~", show_alert=True)
        await show_page(client, parts[1], int(parts[2]), callback=cq)
        return await cq.answer()
    if kind == 'stop':
        gen_id = parts[1] if len(parts) > 1 else cq.inline_message_id
        gen = chat.generations.get(gen_id)
        if gen is None:
            return await cq.answer("❌ 任务已经结束了呢 ~", show_alert=True)
        if uid != gen['owner'] and not is_admin(uid):
            return await cq.answer("❌ 只有发起者或管理员可以停止哦 ~", show_alert=True)
        gen['stop'] = True
        return await cq.answer("✔ 已接收到停止任务指令 任务正在停止中 ~", show_alert=True)
    if kind == 'prune':
        if not is_su(uid):
            return await cq.answer(DENY['su'], show_alert=True)
        prune_state['stop'] = True
        return await cq.answer("✔ 已接收到停止任务指令 任务正在停止中 ~", show_alert=True)
    if kind == 'cfg':
        return await panel.on_callback(client, cq, parts, refresh)
    if kind in ('r', 'm'):
        if uid != int(parts[1]):
            return await cq.answer("❌ 请不要点击别人的按钮哦 ~", show_alert=True)
        action = parts[2]
        if action == 'c':
            if cq.message:
                await _delete_later(client, [cq.message, cq.message.reply_to_message], 0)
            return
        if kind == 'r' and action == 's' and parts[3] in EFFORTS:
            set_user(uid, 'ReasoningEffort', parts[3])
            await cq.edit_message_text(f"✔ 已更换为 `{EFFORTS[parts[3]]}` 思维链深度 ~")
        elif kind == 'm' and action == 'j':
            text, markup = model_panel(uid, int(parts[3]), closable=cq.message is not None)
            return await cq.edit_message_text(text, reply_markup=markup)
        elif kind == 'm' and action == 's':
            names = list(model_map())
            if int(parts[3]) >= len(names):
                return await cq.answer("❌ 模型列表已变化，请重新打开面板 ~", show_alert=True)
            set_user(uid, 'Model', names[int(parts[3])])
            await cq.edit_message_text(f"✔ 已更换为模型 `{names[int(parts[3])]}` ~")
        else:
            return await cq.answer("❌ 未知操作", show_alert=True)
        if cq.message:
            spawn(_delete_later(client, [cq.message, cq.message.reply_to_message], 10))
        return
    await cq.answer("❌ 未知错误", show_alert=True)
