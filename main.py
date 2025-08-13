from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Dict
import os
import uuid
import json

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# Менеджер соединений для хранения активных WebSocket подключений
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.call_offers: Dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception as e:
                print(f"Error sending message to {user_id}: {e}")
                self.disconnect(user_id)

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]


manager = ConnectionManager()


# Маршруты
@app.get("/")
async def home():
    """Главная страница с перенаправлением на чат"""
    user_id = str(uuid.uuid4())[:8]
    return RedirectResponse(url=f"/chat/{user_id}")


@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    """Страница чата"""
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user_id
    })


@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call(request: Request, call_id: str):
    """Страница видеозвонка"""
    return templates.TemplateResponse("call.html", {
        "request": request,
        "call_id": call_id,
        "user_id": call_id.split("_")[0]
    })


# WebSocket endpoint
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """Обработчик WebSocket соединений"""
    await manager.connect(websocket, user_id)
    try:
        while True:
            # Получаем и обрабатываем сообщения
            data = await websocket.receive_json()

            # Обработка текстовых сообщений
            if data.get("type") == "message":
                await manager.send_personal_message({
                    "type": "message",
                    "from": user_id,
                    "message": data["message"]
                }, data["to"])

            # Обработка предложения звонка (offer)
            elif data.get("type") == "offer":
                call_id = f"{user_id}_{data['to']}"
                manager.call_offers[call_id] = data["offer"]
                await manager.send_personal_message({
                    "type": "call_offer",
                    "from": user_id,
                    "offer": data["offer"],
                    "call_id": call_id
                }, data["to"])

            # Обработка ответа на звонок (answer)
            elif data.get("type") == "answer":
                await manager.send_personal_message({
                    "type": "answer",
                    "answer": data["answer"],
                    "call_id": data["call_id"]
                }, data["to"])

            # Обработка ICE кандидатов
            elif data.get("type") == "ice_candidate":
                await manager.send_personal_message({
                    "type": "ice_candidate",
                    "candidate": data["candidate"],
                    "call_id": data["call_id"]
                }, data["to"])

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        print(f"User {user_id} disconnected")
    except json.JSONDecodeError:
        print(f"Invalid JSON received from {user_id}")
    except Exception as e:
        print(f"Unexpected error with {user_id}: {e}")
        manager.disconnect(user_id)


# Запуск сервера
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        workers=1,
        timeout_keep_alive=30,
        log_level="info"
    )
