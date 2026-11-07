# -*- coding: UTF-8 -*-
"""OpenAI Responses API (/v1/responses) 适配：chat 格式的请求转成 Responses 请求，并解析它的流式事件

部分模型（如 gpt-5 系列）在 /chat/completions 下不能同时使用推理与函数工具，必须走这个接口。
无状态使用 (store=false)：推理项（加密）随工具调用一起回传，保证多轮工具调用之间推理连续。
"""

# 推理项挂在 assistant 消息的这个字段上，只在 Responses 请求里使用，chat 请求会把它去掉
REASONING_KEY = '_reasoning'


def _content(content):
    """chat 的 content（字符串或多模态片段）-> Responses 的 content"""
    if not isinstance(content, list):
        return content or ''
    out = []
    for part in content:
        kind = part.get('type')
        if kind == 'text':
            out.append({'type': 'input_text', 'text': part['text']})
        elif kind == 'image_url':
            out.append({'type': 'input_image', 'image_url': part['image_url']['url']})
        elif kind == 'file':
            out.append({'type': 'input_file', **part['file']})
    return out


def to_request(payload: dict) -> dict:
    """chat 格式 (messages / tools / tool_choice / reasoning_effort) -> Responses 请求体（不含 stream）"""
    instructions, items = [], []
    for m in payload['messages']:
        role = m['role']
        if role == 'system':
            instructions.append(m['content'])
        elif role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': m['tool_call_id'], 'output': m['content']})
        elif role == 'assistant':
            items += m.get(REASONING_KEY) or []
            if m.get('content'):
                items.append({'role': 'assistant', 'content': m['content']})
            items += [{'type': 'function_call', 'call_id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']}
                      for c in m.get('tool_calls') or []]
        else:
            items.append({'role': role, 'content': _content(m['content'])})
    body = {'model': payload['model'], 'input': items, 'store': False, 'include': ['reasoning.encrypted_content']}
    if instructions:
        body['instructions'] = '\n\n'.join(instructions)
    if payload.get('tools'):
        # Responses 的函数工具默认 strict，要求所有参数必填且禁止多余字段，这里的工具有可选参数，所以显式关闭
        body['tools'] = [{'type': 'function', 'strict': False, **t['function']} for t in payload['tools']]
    if 'tool_choice' in payload:
        body['tool_choice'] = payload['tool_choice']
    if payload.get('reasoning_effort'):
        body['reasoning'] = {'effort': payload['reasoning_effort']}
    return body


def extract_text(data: dict) -> str:
    """非流式响应里的文本"""
    if isinstance(data.get('output_text'), str):
        return data['output_text']
    return ''.join(p.get('text', '') for item in data.get('output') or [] if item.get('type') == 'message'
                   for p in item.get('content') or [] if p.get('type') == 'output_text')


class Reader:
    """把流式事件累积成 (文本, chat 格式的工具调用, assistant 消息的附加字段)；出错时设置 error"""

    def __init__(self):
        self.text, self.error = '', ''
        self.calls = {}       # item id -> chat 格式的调用，保持出现顺序
        self.reasoning = {}   # item id -> 带加密内容的推理项

    def _item(self, item: dict):
        kind = item.get('type')
        if kind == 'function_call':
            key = item.get('id') or item.get('call_id') or str(len(self.calls))
            call = self.calls.setdefault(key, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
            call['id'] = item.get('call_id') or call['id'] or key
            call['function']['name'] = item.get('name') or call['function']['name']
            if item.get('arguments'):
                call['function']['arguments'] = item['arguments']
        elif kind == 'reasoning' and item.get('encrypted_content'):
            self.reasoning[item.get('id') or str(len(self.reasoning))] = item

    async def feed(self, event: dict, on_text=None):
        kind = event.get('type') or ''
        if kind == 'response.output_text.delta':
            if delta := event.get('delta'):
                self.text += delta
                if on_text:
                    await on_text(delta)
        elif kind == 'response.function_call_arguments.delta':
            if call := self.calls.get(event.get('item_id')):
                call['function']['arguments'] += event.get('delta') or ''
        elif kind in ('response.output_item.added', 'response.output_item.done'):
            self._item(event.get('item') or {})
        elif kind in ('response.completed', 'response.incomplete'):
            output = (event.get('response') or {}).get('output') or []
            for item in output:   # 兜底：没有收到逐项事件的服务，从最终结果里补齐
                self._item(item)
            if not self.text and (text := extract_text({'output': output})):
                self.text = text
                if on_text:
                    await on_text(text)
        elif kind in ('response.failed', 'error'):
            err = (event.get('response') or {}).get('error') or event
            self.error = f"{err.get('code') or 'error'}: {err.get('message') or str(event)[:300]}"

    def result(self) -> tuple[str, list, dict]:
        calls = [c for c in self.calls.values() if c['function']['name']]
        for i, call in enumerate(calls):
            call['id'] = call['id'] or f'call_local_{i}'
        return self.text, calls, {REASONING_KEY: list(self.reasoning.values())} if calls and self.reasoning else {}
