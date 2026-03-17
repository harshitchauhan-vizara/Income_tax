import json
import time
import uuid
from collections import defaultdict, deque

from fastapi import WebSocket, WebSocketDisconnect

from .config import Settings


class WebSocketManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.active_connections: set[WebSocket] = set()
        self.socket_session_map: dict[WebSocket, str] = {}
        self.audio_buffers: dict[str, bytearray] = defaultdict(bytearray)
        self.rate_limit_windows: dict[str, deque[float]] = defaultdict(deque)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)
        self.socket_session_map[websocket] = str(uuid.uuid4())

    def reset_session(self, websocket: WebSocket) -> str:
        old_session_id = self.socket_session_map.get(websocket)
        if old_session_id:
            self.audio_buffers.pop(old_session_id, None)
            self.rate_limit_windows.pop(old_session_id, None)
        new_session_id = str(uuid.uuid4())
        self.socket_session_map[websocket] = new_session_id
        return new_session_id

    def disconnect(self, websocket: WebSocket) -> None:
        session_id = self.socket_session_map.pop(websocket, None)
        self.active_connections.discard(websocket)
        if session_id:
            self.audio_buffers.pop(session_id, None)
            self.rate_limit_windows.pop(session_id, None)

    def get_session_id(self, websocket: WebSocket) -> str:
        return self.socket_session_map[websocket]

    async def send_json(self, websocket: WebSocket, payload: dict) -> bool:
        if "session_id" not in payload and websocket in self.socket_session_map:
            payload["session_id"] = self.socket_session_map[websocket]
        try:
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except WebSocketDisconnect:
            self.disconnect(websocket)
            return False
        except RuntimeError:
            self.disconnect(websocket)
            return False

    async def send_bytes(self, websocket: WebSocket, payload: bytes) -> bool:
        try:
            await websocket.send_bytes(payload)
            return True
        except WebSocketDisconnect:
            self.disconnect(websocket)
            return False
        except RuntimeError:
            self.disconnect(websocket)
            return False

    def append_audio_chunk(self, session_id: str, chunk: bytes) -> None:
        self.audio_buffers[session_id].extend(chunk)

    def pop_audio_buffer(self, session_id: str) -> bytes:
        data = bytes(self.audio_buffers.get(session_id, bytearray()))
        self.audio_buffers[session_id].clear()
        return data

    def allow_request(self, session_id: str) -> bool:
        now = time.time()
        window = self.rate_limit_windows[session_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.settings.rate_limit_per_minute:
            return False
        window.append(now)
        return True
