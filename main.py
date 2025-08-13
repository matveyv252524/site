from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import json

app = FastAPI()
templates = Jinja2Templates(directory="templates")

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.call_offers = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        print(f"User {user_id} connected. Active: {list(self.active_connections.keys())}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def send_json(self, receiver_id: str, message: dict):
        if receiver_id in self.active_connections:
            await self.active_connections[receiver_id].send_json(message)

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            print(f"User {user_id} disconnected")

manager = ConnectionManager()

@app.get("/")
async def home():
    user_id = str(uuid.uuid4())[:8]
    return RedirectResponse(url=f"/chat/{user_id}")

@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user_id
    })

@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call(request: Request, call_id: str):
    return templates.TemplateResponse("call.html", {
        "request": request,
        "call_id": call_id,
        "user_id": call_id.split("_")[0]
    })

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            print(f"Received from {user_id}: {data}")

            if data["type"] == "message":
                await manager.send_json(data["to"], {
                    "type": "message",
                    "from": user_id,
                    "message": data["message"],
                    "timestamp": str(datetime.now())
                })

            elif data["type"] == "call_offer":
                call_id = f"{user_id}_{data['to']}"
                await manager.send_json(data["to"], {
                    "type": "call_offer",
                    "from": user_id,
                    "call_id": call_id,
                    "offer": data["offer"]
                })

            elif data["type"] == "call_answer":
                await manager.send_json(data["to"], {
                    "type": "call_answer",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "answer": data["answer"]
                })

            elif data["type"] == "ice_candidate":
                await manager.send_json(data["to"], {
                    "type": "ice_candidate",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "candidate": data["candidate"]
                })

    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        print(f"Error with {user_id}: {str(e)}")
        manager.disconnect(user_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
