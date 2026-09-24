# -*- coding: UTF-8 -*-
"""对话引擎：普通消息、访客模式、内联模式共用同一套流程，区别只在输出方式 (Sink)"""

import re
import json
import math
import time
import base64
import random
import string
import asyncio
import datetime
import mimetypes
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from loguru import logger
import pyrogram
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, ReplyParameters,
                            InlineQueryResultArticle, InputTextMessageContent)
from .config import config, user_conf, model_map
from . import db, llm, tools, sandbox

me = None  # Bot 自身信息，启动时赋值

# ---------- 翻页与停止 ----------

views: OrderedDict = OrderedDict()   # key -> 完整文本
msg_views: dict = {}                 # "chat_id:msg_id" -> key，供 /page 使用
generations: dict = {}               # 生成任务 id -> {'stop': bool, 'owner': uid}
draft_gens: dict = {}                # 私聊草稿 id -> 生成任务 id，用于草稿自带的停止按钮


_tasks = set()


def spawn(coro):
    """后台运行协程并保留引用，避免任务被回收"""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def rand_id(k=10) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))


def store_view(text: str) -> str:
    key = rand_id(8)
    views[key] = text
    while len(views) > 500:
        views.popitem(last=False)
    return key


def page_count(text: str) -> int:
    return max(1, math.ceil(len(text) / config['Other']['MaxTextLength']))


def page_of(text: str, page: int) -> str:
    size = config['Other']['MaxTextLength']
    return text[(page - 1) * size: page * size]


def page_markup(key: str, page: int, total: int) -> list:
    row = [InlineKeyboardButton('上一页', callback_data=f"pg {key} {page - 1}") if page > 1 else InlineKeyboardButton('      ', callback_data='noop'),
           InlineKeyboardButton(f'{page} / {total}', callback_data=f"pgi {page} {total}"),
           InlineKeyboardButton('下一页', callback_data=f"pg {key} {page + 1}") if page < total else InlineKeyboardButton('      ', callback_data='noop')]
    return [row]


def paged(text: str, page: int = 1) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
    """返回 (当前页文本, 翻页按钮, 视图 key)"""
    total = page_count(text)
    if total == 1:
        return text, None, None
    key = store_view(text)
    return page_of(text, page), InlineKeyboardMarkup(page_markup(key, page, total)), key


def stop_markup(data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton('停止', callback_data=data)]])


def rich(text: str) -> InputRichMessage:
    return InputRichMessage(markdown=text)


def safe_text(text) -> str:
    return unicodedata.normalize("NFC", str(text or ''))


def display_name(user) -> str:
    return safe_text(' '.join(p for p in (user.first_name, user.last_name) if p) or user.username or str(user.id))


def get_delay(edits: int) -> float:
    strategy = config['AI']['ChunkStrategy']
    if strategy['MaxEdit'] <= 1:
        return 0
    progress = edits / strategy['MaxEdit']
    return strategy['Delay']['Base'] + strategy['Delay']['Extra'] * (1 - math.e ** (-progress * strategy['Delay']['Progress']))


def preview(text: str) -> str:
    """流式过程中只展示末尾一页"""
    size = config['Other']['MaxTextLength']
    return text if len(text) <= size else '…' + text[-size:]


async def _try(coro):
    try:
        return await coro
    except pyrogram.errors.MessageNotModified:
        pass
    except Exception as e:
        logger.debug(f"Telegram call failed: {e}")


# ---------- 输出 ----------

class MessageSink:
    """普通消息：私聊用草稿流式输出，群聊编辑消息"""

    def __init__(self, client, message, gen_id):
        self.client, self.message, self.gen_id = client, message, gen_id
        self.chat_id = message.chat.id
        self.private = message.chat.type == pyrogram.enums.ChatType.PRIVATE
        self.msg = None       # 当前状态 / 流式消息
        self.draft_id = client.rnd_id()
        self.limited = not self.private

    async def status(self, text):
        if self.msg:
            await _try(self.client.edit_message_text(self.chat_id, self.msg.id, text, reply_markup=stop_markup(f'stop {self.gen_id}')))
        else:
            self.msg = await _try(self.client.send_message(self.chat_id, text, reply_parameters=ReplyParameters(message_id=self.message.id), reply_markup=stop_markup(f'stop {self.gen_id}')))

    async def stream(self, text):
        if self.private:
            draft_gens[self.draft_id] = self.gen_id
            await _try(self.client.send_rich_message_draft(self.chat_id, self.draft_id, rich(preview(text)), can_stop=True, keep_on_stop=True))
        elif self.msg:
            await _try(self.client.edit_message_text(self.chat_id, self.msg.id, rich_message=rich(preview(text)), reply_markup=stop_markup(f'stop {self.gen_id}')))
        else:
            await self.status('⏳ 思考中 ...')

    async def _final(self, text):
        page, markup, key = paged(text)
        if self.msg and not self.private:
            sent = await _try(self.client.edit_message_text(self.chat_id, self.msg.id, rich_message=rich(page), reply_markup=markup))
            sent = sent or self.msg
        else:
            sent = await _try(self.client.send_rich_message(self.chat_id, rich(page), reply_markup=markup, reply_parameters=ReplyParameters(message_id=self.message.id)))
        if sent and key:
            msg_views[f"{self.chat_id}:{sent.id}"] = key
        self.msg = None if not self.private else self.msg
        self.draft_id = self.client.rnd_id()

    async def commit(self, text):
        await self._final(text)

    async def finish(self, text):
        await self._final(text)
        if self.private and self.msg:
            await _try(self.client.delete_messages(self.chat_id, self.msg.id))

    async def fail(self, text):
        if self.msg:
            await _try(self.client.edit_message_text(self.chat_id, self.msg.id, text))
        else:
            await _try(self.client.send_message(self.chat_id, text, reply_parameters=ReplyParameters(message_id=self.message.id)))


class InlineSink:
    """内联消息（内联模式与访客模式）：只能编辑同一条消息"""
    limited = True

    def __init__(self, client, inline_message_id, stop_data):
        self.client, self.id, self.stop_data = client, inline_message_id, stop_data

    @classmethod
    async def from_guest(cls, client, message, gen_id):
        sent = await client.answer_guest_query(message.guest_query_id, InlineQueryResultArticle(
            '⏳ 思考中 ...', input_message_content=InputTextMessageContent('⏳ 思考中 ...'), reply_markup=stop_markup(f'stop {gen_id}')))
        return cls(client, sent.inline_message_id, f'stop {gen_id}')

    async def status(self, text):
        await _try(self.client.edit_inline_text(self.id, text, reply_markup=stop_markup(self.stop_data)))

    async def stream(self, text):
        await _try(self.client.edit_inline_text(self.id, rich_message=rich(preview(text)), reply_markup=stop_markup(self.stop_data)))

    async def commit(self, text):
        pass

    async def finish(self, text):
        page, markup, _ = paged(text)
        await _try(self.client.edit_inline_text(self.id, rich_message=rich(page), reply_markup=markup))

    async def fail(self, text):
        await _try(self.client.edit_inline_text(self.id, text))


# ---------- 请求 ----------

@dataclass
class Request:
    user: object              # pyrogram User
    conv: str                 # 会话 key（数据库与沙箱目录使用）
    text: str
    group: bool
    history: bool             # 是否读写上下文
    tool_ctx: tools.ToolContext
    message: object = None    # 原始消息，用于读取图片、文件和回复
    gen_id: str = ''


def id_format(name: str, content: str) -> str:
    fmt = config['AI']['IDRecognition']['Format']
    return fmt.replace('{name}', name).replace('{content}', content)


def _media_kind(msg) -> str | None:
    limit = config['AI']['MaxFileSize'] * 1024 * 1024
    if msg.photo:
        return 'image' if msg.photo.file_size < limit else None
    if msg.sticker:
        return 'image' if not (msg.sticker.is_animated or msg.sticker.is_video) and msg.sticker.file_size < limit else None
    if msg.document and msg.document.file_size < limit:
        return 'image' if (msg.document.mime_type or '').startswith('image/') else 'file'
    return None


def _file_name(msg) -> str:
    if msg.document and msg.document.file_name:
        name = msg.document.file_name
    else:
        name = 'sticker.webp' if msg.sticker else 'photo.jpg'
    name = re.sub(r'[/\\\x00]', '_', name).strip('. ') or 'file'
    return f"{msg.id}_{name}"


def _mime(data: bytes, msg) -> str:
    if msg.document and msg.document.mime_type:
        return msg.document.mime_type
    for magic, mime in ((b'\x89PNG', 'image/png'), (b'\xff\xd8', 'image/jpeg'), (b'GIF', 'image/gif')):
        if data.startswith(magic):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return mimetypes.guess_type(_file_name(msg))[0] or 'image/jpeg'


async def collect_media(client, message, req: Request, sink) -> tuple[list, list]:
    """下载消息（含媒体组）中的图片和文件；返回 (多模态内容, 文字说明)"""
    msgs = [message]
    if message.media_group_id:
        try:
            msgs = await client.get_media_group(message.chat.id, message.id)
        except Exception:
            pass
    parts, notes, announced = [], [], False
    for msg in msgs:
        kind = _media_kind(msg)
        if not kind or (kind == 'file' and not (sandbox.enabled() or config['AI']['FileSupport'])):
            continue
        if generations.get(req.gen_id, {}).get('stop'):
            break
        if not announced:
            announced = True
            await sink.status('⏳ 获取文件中 ...')
        try:
            buf = await client.download_media(msg, in_memory=True)
            data = bytes(buf.getbuffer())
        except Exception as e:
            logger.warning(f"Download media failed: {e}")
            continue
        mime = _mime(data, msg)
        if sandbox.enabled():
            path = f"/workspace/chat/uploads/{_file_name(msg)}"
            err = await sandbox.write_file(req.user.id, req.conv, path, data)
            notes.append(f"[文件已保存到 {path}]" if not err else f"[文件保存失败: {err}]")
        if kind == 'image':
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
        elif config['AI']['FileSupport']:
            parts.append({"type": "file", "file": {"filename": _file_name(msg), "file_data": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    return parts, notes


def _content(text: str, parts: list):
    return ([{"type": "text", "text": text}] if text else []) + parts if parts else text


def _tz() -> str:
    now = datetime.datetime.now().astimezone()
    offset = now.strftime('%z')
    return f"{now.tzname()} (UTC{offset[:3]}:{offset[3:]})"


async def system_prompt(req: Request, model: str) -> str:
    settings = user_conf(req.user.id)
    now = datetime.datetime.now()
    parts = []
    if extra := config['AI']['ExtraSystemPrompt']:
        values = {'cur_date': now.strftime("%Y-%m-%d"), 'cur_time': now.strftime("%H:%M:%S"), 'cur_datetime': now.strftime("%Y-%m-%d %H:%M:%S"),
                  'timezone': _tz(), 'model_name': llm.display_name(model), 'assistant_name': me.first_name if me else 'Assistant'}
        parts.append(re.sub(r'\{(\w+)\}', lambda m: str(values.get(m.group(1), m.group(0))), extra))
    if req.group and config['AI']['IDRecognition']['Enable'] and config['AI']['IDRecognition']['Prompt']:
        parts.append(config['AI']['IDRecognition']['Prompt'])
    if prompt := settings['SystemPrompt'] or config['AI']['SystemPrompt']:
        parts.append(prompt)
    if config['AI']['Memory']['Enable']:
        memories = [('user', m) for m in await db.list_memories(f"user:{req.user.id}")] + \
                   [('chat', m) for m in await db.list_memories(f"chat:{req.conv}")]
        if memories:
            parts.append("Long-term memories (manage with memory_save / memory_delete):\n" +
                         "\n".join(f"- [{scope}] {m['topic']}: {m['content']}" for scope, m in memories))
    if req.history and config['AI']['Summary']['Enable'] and (summary := await db.get_summary(req.conv)):
        parts.append(f"Summary of the earlier conversation:\n{summary}")
    return "\n\n".join(parts)


async def build_messages(req: Request, model: str, parts: list, notes: list, reply: tuple | None) -> tuple[list, str]:
    """返回 (发给模型的消息, 需要存入历史的用户消息文本)"""
    idr = req.group and config['AI']['IDRecognition']['Enable']
    messages = []
    if system := await system_prompt(req, model):
        messages.append({"role": "system", "content": system})
    if req.history:
        rows = await db.recent(req.conv, config['AI']['MaxContext'], unsummarized_only=config['AI']['Summary']['Enable'])
        messages += [{"role": r['role'], "content": r['content']} for r in rows]
    if reply:
        name, text, reply_parts = reply
        messages.append({"role": "user", "content": _content(id_format(name, text) if idr else text, reply_parts)})
    text = "\n".join([req.text] + notes) if notes else req.text
    user_text = id_format(display_name(req.user), text) if idr else text
    messages.append({"role": "user", "content": _content(user_text, parts)})
    return messages, user_text + ("\n[图片]" * sum(p['type'] == 'image_url' for p in parts))


def model_candidates(uid) -> list:
    configured = model_map()
    first = user_conf(uid)['Model']
    if first not in configured:
        first = config['AI']['DefaultModel']
    return [m for m in dict.fromkeys([first] + config['AI']['FallbackModel']) if m in configured]


async def run(req: Request, sink):
    """执行一次对话：构建上下文 -> 流式请求 -> 工具调用循环 -> 输出与保存"""
    gen = generations.setdefault(req.gen_id, {'stop': False, 'owner': req.user.id})
    stopped = lambda: gen['stop']
    try:
        models = model_candidates(req.user.id)
        if not models:
            return await sink.fail("❌ 还没有配置可用的模型呢 ~")
        parts, notes, reply = [], [], None
        if req.message:
            parts, notes = await collect_media(req.tool_ctx.client, req.message, req, sink)
            replied = req.message.reply_to_message
            if replied and replied.from_user and not replied.from_user.is_self:
                r_parts, r_notes = await collect_media(req.tool_ctx.client, replied, req, sink)
                r_text = "\n".join([safe_text(replied.text or replied.caption)] + r_notes).strip()
                if r_text or r_parts:
                    reply = (display_name(replied.from_user), r_text, r_parts)
        if stopped():
            return await sink.fail("❌ 已停止 ~")
        messages, history_text = await build_messages(req, models[0], parts, notes, reply)
        payload = {"messages": messages}
        if tool_defs := tools.available(req.tool_ctx):
            payload.update(tools=tool_defs, tool_choice="auto")
        effort = user_conf(req.user.id)['ReasoningEffort'] or config['AI']['ReasoningEffort']
        if effort not in ('', 'auto'):
            payload['reasoning_effort'] = effort
        logger.info(f"User {req.user.id} chat in {req.conv} with {models[0]}")

        await sink.status('⏳ 思考中 ...')
        answers, text, attempt, edits = [], '', 0, 0
        for round_ in range(config['AI']['Tool']['MaxToolCall'] + 1):
            if round_ == config['AI']['Tool']['MaxToolCall']:
                payload.pop('tools', None)
                payload.pop('tool_choice', None)
            while True:
                buf = {'text': '', 'pending': '', 'last': 0}

                async def on_text(delta):
                    nonlocal edits
                    buf['text'] += delta
                    buf['pending'] += delta
                    now = time.time()
                    if (sink.limited and edits >= config['AI']['ChunkStrategy']['MaxEdit']) or now - buf['last'] < get_delay(edits):
                        return
                    if re.search(config['AI']['ChunkStrategy']['Rule'], buf['pending']):
                        buf['last'], buf['pending'] = now, ''
                        edits += 1
                        await sink.stream(buf['text'])

                model = models[attempt % len(models)]
                try:
                    text, calls = await llm.stream(model, payload, on_text, stopped)
                    break
                except Exception as e:
                    attempt += 1
                    logger.warning(f"Model {model} failed: {e}")
                    if attempt > config['Network']['Retry'] or stopped():
                        return await sink.fail(f"❌ 获取消息失败呢 ~\n\n错误: `{str(e)[:300]}`")
                    await sink.status(f"⏳ 重试中 ({attempt}/{config['Network']['Retry']}) ...")
            if stopped() or not calls:
                break
            if text:
                answers.append(text)
                await sink.commit(text)
            payload['messages'].append({"role": "assistant", "content": text or None, "tool_calls": calls})
            for call in calls:
                if stopped():
                    break
                try:
                    args = json.loads(call['function']['arguments'] or '{}')
                    args = args if isinstance(args, dict) else {}
                except ValueError:
                    args = {}
                await sink.status(tools.status_text(call['function']['name'], args))
                result = await tools.execute(call['function']['name'], args, req.tool_ctx)
                payload['messages'].append({"role": "tool", "tool_call_id": call['id'], "content": result})
            text = ''
        if not text:
            return await sink.fail("✔ 已停止生成 ~" if stopped() else "❌ 获取消息失败呢 ~")
        await sink.finish(text)
        if req.history:
            keep = config['AI']['Memory']['MaxHistory']
            await db.add_message(req.conv, 'user', history_text, display_name(req.user), keep)
            await db.add_message(req.conv, 'assistant', "\n\n".join(answers + [text]), None, keep)
            spawn(summarize(req.conv, models[0]))
    finally:
        generations.pop(req.gen_id, None)
        for draft in [k for k, v in draft_gens.items() if v == req.gen_id]:
            draft_gens.pop(draft, None)


# ---------- 滚动摘要 ----------

_summarizing = set()

SUMMARY_PROMPT = ("You maintain a running summary of a chat between users and an AI assistant. Merge the new messages into the existing summary. "
                  "Keep facts, decisions, user preferences, names, files and unfinished tasks; drop small talk. "
                  "Write in the main language of the conversation, at most 300 words. Output only the summary.")


async def summarize(conv: str, model: str):
    conf = config['AI']['Summary']
    if not conf['Enable'] or conv in _summarizing:
        return
    _summarizing.add(conv)
    try:
        limit = max(conf['MaxMessage'], 4)
        pending = await db.count(conv, unsummarized_only=True)
        if pending <= limit:
            return
        rows = (await db.recent(conv, pending, unsummarized_only=True))[:pending - limit // 2]
        old = await db.get_summary(conv)
        lines = "\n".join(f"{r['role']}: {r['content'][:2000]}" for r in rows)
        summary = await llm.complete(conf['Model'] or model, [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Existing summary:\n{old or '(none)'}\n\nNew messages:\n{lines}"},
        ])
        if summary.strip():
            await db.set_summary(conv, summary.strip(), rows[-1]['id'])
            logger.info(f"Summarized {len(rows)} messages in {conv}")
    except Exception as e:
        logger.warning(f"Summarize {conv} failed: {e}")
    finally:
        _summarizing.discard(conv)


# ---------- 三种入口 ----------

def chat_conv(message) -> tuple[str, int]:
    """访客模式下私聊的会话 id 由双方 id 组合而成"""
    chat_id = message.chat.id
    if message.chat.type == pyrogram.enums.ChatType.PRIVATE and chat_id != message.from_user.id:
        chat_id = int(f"{min(chat_id, message.from_user.id)}{max(chat_id, message.from_user.id)}")
    return str(chat_id), chat_id


async def from_message(client, message, text: str):
    private = message.chat.type == pyrogram.enums.ChatType.PRIVATE
    history = user_conf(message.from_user.id)['Context'] or not private
    gen_id = rand_id()
    conv = str(message.chat.id)
    req = Request(message.from_user, conv, safe_text(text), not private, history,
                  tools.ToolContext(client, message.from_user.id, conv, message.chat.id, message.id, history), message, gen_id)
    await run(req, MessageSink(client, message, gen_id))


async def from_guest(client, message, text: str):
    conv, _ = chat_conv(message)
    private = message.chat.type == pyrogram.enums.ChatType.PRIVATE
    history = user_conf(message.from_user.id)['Context'] or not private
    gen_id = rand_id()
    sink = await InlineSink.from_guest(client, message, gen_id)
    req = Request(message.from_user, conv, safe_text(text), not private, history,
                  tools.ToolContext(client, message.from_user.id, conv, None, None, history), message, gen_id)
    await run(req, sink)


async def from_inline(client, chosen):
    uid = chosen.from_user.id
    history = user_conf(uid)['Context']
    conv = f"inline:{uid}"
    gen_id = chosen.inline_message_id
    req = Request(chosen.from_user, conv, safe_text(chosen.query), False, history,
                  tools.ToolContext(client, uid, conv, None, None, history), None, gen_id)
    await run(req, InlineSink(client, chosen.inline_message_id, 'stop'))
