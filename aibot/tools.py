# -*- coding: UTF-8 -*-
"""模型可调用的工具：网页搜索/抓取、沙箱 shell 与文件、长期记忆"""

import io
import json
import urllib.parse
from dataclasses import dataclass
from bs4 import BeautifulSoup
from loguru import logger
from curl_cffi.requests import AsyncSession
from pyrogram.types import ReplyParameters
from .config import config
from . import db, sandbox

_proxy_pos = 0

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Sec-Ch-Ua": "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": "\"Windows\"",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class ToolContext:
    client: object
    user_id: int
    conv: str
    chat_id: int | None = None   # 可以直接发送文件的聊天，内联/访客模式为 None
    reply_to: int | None = None
    history: bool = True


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


def _str(description):
    return {"type": "string", "description": description}


DEFINITIONS = {
    "web_search": _fn("web_search", "Search the web for current information, news and real-time content.",
                      {"query": _str("The search query string.")}, ["query"]),
    "web_fetch": _fn("web_fetch", "Fetch the text content of a web page by URL.",
                     {"url": _str("The URL of the web page to fetch.")}, ["url"]),
    "shell": _fn("shell", (
        "Run a bash command in a persistent Linux sandbox and return its output. "
        "The working directory /workspace/chat is shared by everyone in this chat; /workspace/user (HOME) is private to the current user "
        "and shared across chats. Both persist between calls; everything else (including /tmp) is reset after each call. "
        "Python3 and common CLI tools are available; `pip install --user` installs into HOME. Files sent by users are saved to /workspace/chat/uploads/."),
        {"command": _str("The bash command to run.")}, ["command"]),
    "write_file": _fn("write_file", "Create or overwrite a text file in the sandbox. Relative paths are resolved against /workspace/chat.",
                      {"path": _str("File path under /workspace/chat or /workspace/user."), "content": _str("Full file content.")}, ["path", "content"]),
    "send_file": _fn("send_file", "Send a file from the sandbox to the current Telegram chat, e.g. a generated image, document or archive.",
                     {"path": _str("File path under /workspace/chat or /workspace/user."), "caption": _str("Optional caption.")}, ["path"]),
    "memory_save": _fn("memory_save", (
        "Save a long-term memory that will be shown to you in future conversations. Use it for stable facts and preferences worth remembering, "
        "not for transient details. Saving an existing topic overwrites it."),
        {"topic": _str("Short unique key, e.g. 'preferred_language'."), "content": _str("What to remember."),
         "scope": {"type": "string", "enum": ["user", "chat"], "description": "'user' = about the current user (default); 'chat' = shared by this chat."}},
        ["topic", "content"]),
    "memory_delete": _fn("memory_delete", "Delete a long-term memory by topic.",
                         {"topic": _str("The topic to delete."), "scope": {"type": "string", "enum": ["user", "chat"]}}, ["topic"]),
    "memory_search": _fn("memory_search", "Full-text search older messages of this conversation, including ones no longer in context.",
                         {"query": _str("Keywords to search for (space separated, any match).")}, ["query"]),
}


def available(ctx: ToolContext) -> list[dict]:
    tool = config['AI']['Tool']
    if not tool['Enable']:
        return []
    names = []
    if tool['WebSearch']['Enable']:
        names.append('web_search')
    if tool['FetchURL']['Enable']:
        names.append('web_fetch')
    if sandbox.enabled():
        names += ['shell', 'write_file'] + (['send_file'] if ctx.chat_id is not None else [])
    if config['AI']['Memory']['Enable']:
        names += ['memory_save', 'memory_delete'] + (['memory_search'] if ctx.history else [])
    return [DEFINITIONS[n] for n in names]


def status_text(name: str, args: dict) -> str:
    def short(s, n=60):
        s = str(s).replace('`', "'").replace('\n', ' ')
        return s if len(s) <= n else s[:n] + '…'
    return {
        'web_search': f"⏳ 搜索 `{short(args.get('query', ''))}` 中 ...",
        'web_fetch': f"⏳ 请求 `{short(args.get('url', ''))}` 中 ...",
        'shell': f"⏳ 执行 `{short(args.get('command', ''))}` 中 ...",
        'write_file': f"⏳ 写入 `{short(args.get('path', ''))}` 中 ...",
        'send_file': f"⏳ 发送 `{short(args.get('path', ''))}` 中 ...",
        'memory_search': f"⏳ 回忆 `{short(args.get('query', ''))}` 中 ...",
    }.get(name, "⏳ 整理记忆中 ...")


def memory_scope(ctx: ToolContext, scope: str = 'user') -> str:
    return f"chat:{ctx.conv}" if scope == 'chat' else f"user:{ctx.user_id}"


async def execute(name: str, args: dict, ctx: ToolContext) -> str:
    try:
        if name == 'web_search':
            return await web_search(args.get('query', ''))
        if name == 'web_fetch':
            return await web_fetch(args.get('url', ''))
        if name == 'shell':
            return await sandbox.shell(ctx.user_id, ctx.conv, args.get('command', ''))
        if name == 'write_file':
            err = await sandbox.write_file(ctx.user_id, ctx.conv, args.get('path', ''), str(args.get('content', '')).encode())
            return f"Error: {err}" if err else f"Wrote {args.get('path')}"
        if name == 'send_file':
            return await send_file(ctx, args.get('path', ''), args.get('caption', ''))
        if name == 'memory_save':
            topic, content = str(args.get('topic', '')).strip()[:100], str(args.get('content', '')).strip()[:2000]
            if not topic or not content:
                return "Error: topic and content are required."
            await db.save_memory(memory_scope(ctx, args.get('scope', 'user')), topic, content)
            return f"Saved memory '{topic}'."
        if name == 'memory_delete':
            ok = await db.delete_memory(memory_scope(ctx, args.get('scope', 'user')), str(args.get('topic', '')))
            return "Deleted." if ok else "No such memory."
        if name == 'memory_search':
            rows = await db.search(ctx.conv, str(args.get('query', '')))
            if not rows:
                return "No matching messages."
            return "\n\n".join(f"[{r['role']}{'/' + r['name'] if r['name'] else ''}] {sandbox.clip(r['content'], 800)}" for r in reversed(rows))
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return f"Tool execution error: {e}"


async def send_file(ctx: ToolContext, path: str, caption: str = '') -> str:
    data, err = await sandbox.read_file(ctx.user_id, ctx.conv, path, 50 * 1024 * 1024)
    if data is None:
        return f"Error: {err}"
    buf = io.BytesIO(data)
    buf.name = path.rstrip('/').rsplit('/', 1)[-1] or 'file'
    await ctx.client.send_document(ctx.chat_id, buf, caption=str(caption)[:1024], file_name=buf.name,
                                   reply_parameters=ReplyParameters(message_id=ctx.reply_to) if ctx.reply_to else None)
    return f"Sent {buf.name} ({len(data)} bytes) to the chat."


# ---------- 网络 ----------

def get_proxy():
    global _proxy_pos
    proxies = config['Network']['Proxy']
    if not proxies:
        return None
    _proxy_pos = (_proxy_pos + 1) % len(proxies)
    return proxies[_proxy_pos]


async def request(method: str, url: str, headers: dict = None, body=None):
    """带重试的 HTTP 请求，失败返回 None"""
    for _ in range(config['Network']['Retry'] + 1):
        try:
            async with AsyncSession(impersonate="chrome", timeout=config['Network']['Timeout']) as session:
                return await session.request(method, url, headers=headers, data=body, proxy=get_proxy())
        except Exception as e:
            logger.debug(f"Request {url} failed: {e}")
    return None


async def web_fetch(url: str) -> str:
    if not url:
        return "Error: No URL provided."
    res = await request("GET", url, {**BROWSER_HEADERS, "User-Agent": config['Network']['OverridedUA']})
    if res is None:
        return f"Error: Failed to fetch {url} after retries."
    content_type = res.headers.get("content-type", "")
    text = res.text
    if "text/html" in content_type:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    if len(text) > 15000:
        text = text[:15000] + "\n...[content truncated]"
    return f"Fetched content from {url} (status {res.status_code}, type: {content_type}):\n\n{text}"


async def search_duckduckgo(query: str) -> str | None:
    logger.info(f"Use DuckDuckGo Search {query}")
    res = await request("GET", "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query),
                        {**BROWSER_HEADERS, "Referer": "https://html.duckduckgo.com/", "User-Agent": config['Network']['OverridedUA']})
    if res is None:
        return None
    results = []
    for i, item in enumerate(BeautifulSoup(res.text, "html.parser").select(".result")[:8]):
        title = item.select_one(".result__title a, .result__a")
        snippet = item.select_one(".result__snippet")
        link = title.get("href", "") if title else ""
        if not link and item.select_one(".result__url"):
            link = item.select_one(".result__url").get_text(strip=True)
        link = urllib.parse.unquote(link.replace('//duckduckgo.com/l/?uddg=', '').split('&rut=')[0])
        results.append(f"{i + 1}. {title.get_text(strip=True) if title else 'No title'}\n   URL: {link}\n   {snippet.get_text(strip=True) if snippet else ''}")
    return f"Search results for '{query}':\n\n" + "\n\n".join(results) if results else None


async def search_tavily(query: str) -> str | None:
    key = config['AI']['Tool']['WebSearch']['ApiKey']
    if not key:
        return None
    logger.info(f"Use Tavily Search {query}")
    body = json.dumps({"api_key": key, "query": query, "search_depth": "basic", "include_answer": True, "max_results": 8})
    res = await request("POST", "https://api.tavily.com/search", {"Content-Type": "application/json"}, body)
    try:
        data = res.json()
    except Exception:
        return None
    parts = [f"Summary: {data['answer']}"] if data.get("answer") else []
    for i, r in enumerate(data.get("results", [])):
        parts.append(f"{i + 1}. {r.get('title', 'No title')}\n   URL: {r.get('url', '')}\n   {r.get('content', '')}")
    return f"Search results for '{query}':\n\n" + "\n\n".join(parts) if parts else None


async def web_search(query: str) -> str:
    if not query:
        return "Error: No query provided."
    for engine in (search_duckduckgo, search_tavily):
        if result := await engine(query):
            return result
    return f"No results found for '{query}'"
