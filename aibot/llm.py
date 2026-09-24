# -*- coding: UTF-8 -*-
"""OpenAI 兼容 /chat/completions 客户端：流式输出、工具调用收集、故障转移"""

import json
from loguru import logger
from curl_cffi.requests import AsyncSession
from .config import config, model_map
from .tools import get_proxy

STREAM_IDLE_TIMEOUT = 120


class LLMError(Exception):
    pass


def resolve(name: str) -> tuple[str, dict, str]:
    """模型名 -> (接口地址, 请求头, 实际模型名)；名称中的 `前缀:` 会被去掉"""
    vendor = model_map().get(name)
    if vendor is None:
        raise LLMError(f"模型 {name} 未配置")
    conf = config['AI']['Vendor'][vendor]
    url = f"{conf['BaseUrl'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {conf.get('Key', '')}", "Content-Type": "application/json"}
    return url, headers, name.split(':', 1)[1] if ':' in name else name


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


async def stream(model: str, payload: dict, on_text=None, should_stop=None) -> tuple[str, list]:
    """流式请求一次，返回 (文本, 工具调用)。on_text(增量) 为协程回调"""
    url, headers, real_name = resolve(model)
    payload = {**payload, "model": real_name, "stream": True}
    valid = {t["function"]["name"] for t in payload.get("tools", [])}
    collector, text, pending = ToolCollector(valid), "", ""
    async with AsyncSession() as session:
        async with session.stream("POST", url, headers={**headers, "Accept": "text/event-stream"}, json=payload,
                                  timeout=(config['Network']['Timeout'], STREAM_IDLE_TIMEOUT), impersonate="chrome", proxy=get_proxy()) as resp:
            if resp.status_code >= 400:
                body = (await resp.atext())[:500]
                raise LLMError(f"HTTP {resp.status_code}: {body}")
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
                    if event.get("error"):
                        raise LLMError(str(event["error"])[:500])
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    for tc in delta.get("tool_calls") or []:
                        collector.add(tc)
                    if content := delta.get("content"):
                        text += content
                        if on_text:
                            await on_text(content)
    return text, collector.result()


async def complete(model: str, messages: list, timeout: float = 120, **extra) -> str:
    """非流式请求，返回文本"""
    url, headers, real_name = resolve(model)
    async with AsyncSession() as session:
        resp = await session.post(url, headers=headers, json={"model": real_name, "messages": messages, **extra},
                                  timeout=timeout, impersonate="chrome", proxy=get_proxy())
    if resp.status_code >= 400:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError(f"无法解析的响应: {resp.text[:500]}")
