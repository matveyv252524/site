from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Dict
import uuid
import os

app = FastAPI()

# Создаем необходимые директории
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Хранилища данных
active_connections: Dict[str, WebSocket] = {}
call_offers: Dict[str, dict] = {}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user_id = str(uuid.uuid4())[:8]
    return RedirectResponse(url=f"/chat/{user_id}")


@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    return templates.TemplateResponse("chat.html", {"request": request, "user_id": user_id})


@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call(request: Request, call_id: str):
    return templates.TemplateResponse("call.html", {"request": request, "call_id": call_id})


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    active_connections[user_id] = websocket

    try:
        while True:
            data = await websocket.receive_json()

            if data["type"] == "offer":
                # Сохраняем оффер для вызываемого пользователя
                call_offers[data["call_id"]] = {
                    "from": user_id,
                    "offer": data["offer"]
                }

                # Отправляем уведомление о звонке
                if data["to"] in active_connections:
                    await active_connections[data["to"]].send_json({
                        "type": "incoming_call",
                        "from": user_id,
                        "call_id": data["call_id"]
                    })

            elif data["type"] == "answer":
                # Пересылаем ответ вызывающему
                if data["to"] in active_connections:
                    await active_connections[data["to"]].send_json({
                        "type": "call_answer",
                        "answer": data["answer"],
                        "call_id": data["call_id"]
                    })

            elif data["type"] == "ice_candidate":
                # Пересылаем ICE-кандидата
                if data["to"] in active_connections:
                    await active_connections[data["to"]].send_json({
                        "type": "ice_candidate",
                        "candidate": data["candidate"],
                        "call_id": data["call_id"]
                    })

            elif data["type"] == "message":
                # Пересылаем сообщение
                if data["to"] in active_connections:
                    await active_connections[data["to"]].send_json({
                        "type": "message",
                        "from": user_id,
                        "message": data["message"]
                    })

    except WebSocketDisconnect:
        if user_id in active_connections:
            del active_connections[user_id]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)  # Теперь сайт будет на http://127.0.0.1:8080