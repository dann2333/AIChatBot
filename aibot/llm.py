# -*- coding: UTF-8 -*-
"""OpenAI 兼容客户端：默认 /responses，可选 /chat/completions；流式输出、工具调用收集"""

import json
from loguru import logger
from curl_cffi.requests import AsyncSession
from .config import config, model_map
from .tools import get_proxy
from . import responses

STREAM_IDLE_TIMEOUT = 120


class LLMError(Exception):
    def __init__(self, message, status=0, body=''):
        super().__init__(message)
        self.status, self.body = status, body


# 默认走 Responses 接口（推理与工具可同时使用）；提供商可用 Api: chat 改走 /chat/completions。
# 服务不认识某个可选参数（如 include）时，按报错丢弃该参数重试，并记住（按 接口地址+模型），之后的请求直接不带
_quirks: dict = {}
# 请求体里的可选参数 -> 报错信息里除 param 字段外可能出现的写法
OPTIONAL = {'reasoning_effort': ('reasoning_effort',), 'reasoning': ('reasoning.effort',), 'tool_choice': ('tool_choice',), 'include': (), 'store': ()}


def _unsupported(body: dict, err: LLMError) -> str | None:
    """从 400 报错里找出服务不认识的可选参数；找不到返回 None"""
    try:
        info = json.loads(err.body)['error']
    except (ValueError, KeyError, TypeError):
        info = {}
    info = info if isinstance(info, dict) else {}
    message = str(info.get('message') or err.body).lower()
    if err.status != 400 or '/responses' in message:
        return None    # 指向 /responses 的报错是接口选择问题（如推理与工具不能同用），不能靠丢弃参数绕过
    param = str(info.get('param') or '')
    for key, hints in OPTIONAL.items():
        if key in body and (param.split('.')[0] == key or any(h in message for h in hints)):
            return key
    return None


def _drop(key: tuple, param: str) -> bool:
    dropped = _quirks.setdefault(key, set())
    new = param not in dropped
    dropped.add(param)
    return new


def _adapt(key: tuple, body: dict) -> dict:
    dropped = _quirks.get(key)
    if not dropped:
        return body
    out = {k: v for k, v in body.items() if k not in dropped}
    if 'tool_choice' in dropped and body.get('tool_choice') == 'none':
        out.pop('tools', None)    # 不支持 tool_choice=none 的服务：禁用工具时直接不给工具
    return out


def resolve(name: str) -> tuple[str, dict, str, str]:
    """模型名 -> (API 根地址, 请求头, 实际模型名, 接口类型)；名称中的 `前缀:` 会被去掉。
    接口类型由提供商的 Api 决定：chat 或 responses（默认）"""
    vendor = model_map().get(name)
    if vendor is None:
        raise LLMError(f"模型 {name} 未配置")
    conf = config['AI']['Vendor'][vendor]
    headers = {"Authorization": f"Bearer {conf.get('Key', '')}", "Content-Type": "application/json"}
    api = 'chat' if str(conf.get('Api') or '').lower() == 'chat' else 'responses'
    return conf['BaseUrl'].rstrip('/'), headers, name.split(':', 1)[1] if ':' in name else name, api


def _hint(err: LLMError, api: str) -> LLMError:
    """Responses 端点不存在时，提示改用 chat 接口"""
    if api == 'responses' and err.status in (404, 405, 501):
        return LLMError(f"{err} (该服务可能不支持 /responses，可在提供商下设置 Api: chat)", err.status, err.body)
    return err


def display_name(name: str) -> str:
    return name.split(':', 1)[1] if ':' in name else name


class ToolCollector:
    """把流式 tool_calls 增量拼成完整调用；优先按 id 分组，其次按 index"""

    def __init__(self, valid_names: set):
        self.valid = valid_names
        self.calls = []
        self.by_id, self.by_index = {}, {}

    def add(self, delta: dict):
        tc_id, idx = delta.get("id"), delta.get("index")
        if tc_id and tc_id in self.by_id:
            call = self.by_id[tc_id]
        elif not tc_id and idx is not None and idx in self.by_index:
            call = self.by_index[idx]
        elif not tc_id and idx is None and self.calls:
            call = self.calls[-1]
        else:
            call = {"id": tc_id or "", "type": "function", "function": {"name": "", "arguments": ""}}
            self.calls.append(call)
            if tc_id:
                self.by_id[tc_id] = call
            if idx is not None:
                self.by_index[idx] = call
        fn = delta.get("function") or {}
        call["function"]["name"] += fn.get("name") or ""
        call["function"]["arguments"] += fn.get("arguments") or ""

    def _split_names(self, name: str) -> list:
        """部分服务会把并行调用拼成 web_searchweb_fetch，这里拆开"""
        out = []
        while name:
            match = next((n for n in sorted(self.valid, key=len, reverse=True) if name.startswith(n)), None)
            if not match:
                return []
            out.append(match)
            name = name[len(match):]
        return out

    @staticmethod
    def _split_args(text: str) -> list:
        out, pos, decoder = [], 0, json.JSONDecoder()
        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            try:
                obj, pos = decoder.raw_decode(text, pos)
            except ValueError:
                return []
            out.append(json.dumps(obj, ensure_ascii=False))
        return out

    def result(self) -> list[dict]:
        out, seen = [], set()
        for i, call in enumerate(self.calls):
            name, args = call["function"]["name"], call["function"]["arguments"]
            if not name and not args:
                continue
            call["id"] = call["id"] or f"call_local_{i}"
            if name in self.valid:
                parts = [call]
            else:
                names, argv = self._split_names(name), self._split_args(args)
                if len(names) < 2 or len(names) != len(argv):
                    logger.warning(f"Drop invalid tool call: {call}")
                    continue
                parts = [{"id": call["id"] if j == 0 else f"{call['id']}_{j}", "type": "function",
                          "function": {"name": n, "arguments": a}} for j, (n, a) in enumerate(zip(names, argv))]
            for p in parts:
                mark = (p["function"]["name"], p["function"]["arguments"])
                if mark not in seen:
                    seen.add(mark)
                    out.append(p)
        return out


class ChatReader:
    """/chat/completions 的流式事件 -> (文本, 工具调用, 附加字段)"""

    def __init__(self, valid_names: set):
        self.collector, self.text, self.error = ToolCollector(valid_names), '', ''

    async def feed(self, event: dict, on_text=None):
        if event.get("error"):
            self.error = str(event["error"])
            return
        choices = event.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            self.collector.add(tc)
        if content := delta.get("content"):
            self.text += content
            if on_text:
                await on_text(content)

    def result(self) -> tuple[str, list, dict]:
        return self.text, self.collector.result(), {}


def _chat_body(payload: dict) -> dict:
    # 以 _ 开头的是 Responses 专用的附加字段（如加密的推理项），chat 请求里不带
    messages = [{k: v for k, v in m.items() if not k.startswith('_')} for m in payload['messages']]
    return {**payload, "messages": messages, "stream": True}


async def stream(model: str, payload: dict, on_text=None, should_stop=None) -> tuple[str, list, dict]:
    """流式请求，返回 (文本, 工具调用, assistant 消息的附加字段)。on_text(增量) 为协程回调。
    payload 为 chat 格式；走 Responses 接口时会自动转换，附加字段需原样放进对应的 assistant 消息，推理才能在工具调用间延续"""
    base, headers, real_name, api = resolve(model)
    key = (base, real_name)
    payload = {**payload, "model": real_name}
    while True:
        if api == 'responses':
            url, body, reader = f"{base}/responses", {**responses.to_request(payload), "stream": True}, responses.Reader()
            body = _adapt(key, body)
        else:
            url, body = f"{base}/chat/completions", _adapt(key, _chat_body(payload))
            reader = ChatReader({t["function"]["name"] for t in body.get("tools", [])})
        try:
            return await _run(url, headers, body, reader, on_text, should_stop)
        except LLMError as e:
            param = _unsupported(body, e)
            if not param or not _drop(key, param):
                raise _hint(e, api)
            logger.warning(f"Dropping unsupported parameter {param} for {real_name}: {str(e)[:200]}")


async def _run(url: str, headers: dict, body: dict, reader, on_text, should_stop) -> tuple[str, list, dict]:
    pending = ""
    async with AsyncSession() as session:
        async with session.stream("POST", url, headers={**headers, "Accept": "text/event-stream"}, json=body,
                                  timeout=(config['Network']['Timeout'], STREAM_IDLE_TIMEOUT), impersonate="chrome", proxy=get_proxy()) as resp:
            if resp.status_code >= 400:
                text = await resp.atext()
                raise LLMError(f"HTTP {resp.status_code}: {text[:500]}", resp.status_code, text)
            async for chunk in resp.aiter_content():
                if should_stop and should_stop():
                    break
                pending += chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else chunk
                *lines, pending = pending.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    await reader.feed(event, on_text)
                    if reader.error:
                        raise LLMError(reader.error[:500])
    return reader.result()


async def complete(model: str, messages: list, timeout: float = 120) -> str:
    """非流式请求，返回文本"""
    base, headers, real_name, api = resolve(model)
    key = (base, real_name)
    while True:
        if api == 'responses':
            url, body = f"{base}/responses", responses.to_request({"model": real_name, "messages": messages})
            body.pop('include')
        else:
            url, body = f"{base}/chat/completions", {"model": real_name, "messages": messages}
        body = _adapt(key, body)
        async with AsyncSession() as session:
            resp = await session.post(url, headers=headers, json=body, timeout=timeout, impersonate="chrome", proxy=get_proxy())
        if resp.status_code < 400:
            break
        err = LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}", resp.status_code, resp.text)
        param = _unsupported(body, err)
        if not param or not _drop(key, param):
            raise _hint(err, api)
    try:
        data = resp.json()
        return responses.extract_text(data) if api == 'responses' else data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError(f"无法解析的响应: {resp.text[:500]}")
