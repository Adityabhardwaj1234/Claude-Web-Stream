import httpx
from typing import Iterator


class ChatCompletions:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    def create(self, *, model=None, messages=None, stream=False, effort=None, conversation_id=None, **kw):
        body = {"model": model, "messages": messages or [], "stream": stream, "effort": effort, "conversation_id": conversation_id}
        body.update(kw)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if not stream:
            r = httpx.post(self.base_url + "/v1/chat/completions", json=body, headers=headers, timeout=300)
            r.raise_for_status()
            return r.json()
        # streaming
        with httpx.stream("POST", self.base_url + "/v1/chat/completions", json=body, headers=headers, timeout=300) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    import json
                    yield json.loads(data)


class Chat:
    def __init__(self, base_url, api_key):
        self.completions = ChatCompletions(base_url, api_key)


class Client:
    def __init__(self, base_url="http://127.0.0.1:8787", api_key=None):
        self.base_url = base_url
        self.api_key = api_key
        self.chat = Chat(base_url, api_key)