from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import json


app = FastAPI()

# Убедимся, что пути к шаблонам указаны правильно
current_dir = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(current_dir, "templates"))

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.call_offers = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        print(f"User {user_id} connected. Active connections: {list(self.active_connections.keys())}")

    async def send_message(self, sender: str, receiver: str, message: str):
        print(f"Attempting to send message from {sender} to {receiver}")
        if receiver in self.active_connections:
            try:
                await self.active_connections[receiver].send_json({
                    "type": "message",
                    "from": sender,
                    "message": message,
                    "timestamp": str(datetime.now())
                })
                print(f"Message sent successfully to {receiver}")
                return True
            except Exception as e:
                print(f"Error sending message to {receiver}: {str(e)}")
                del self.active_connections[receiver]
                return False
        else:
            print(f"Receiver {receiver} not found in active connections")
            return False

    async def send_call_offer(self, from_user: str, to_user: str, call_id: str, offer: dict = None):
        if to_user in self.active_connections:
            await self.active_connections[to_user].send_json({
                "type": "call_offer",
                "from": from_user,
                "call_id": call_id,
                "offer": offer
            })

    async def send_call_answer(self, from_user: str, to_user: str, call_id: str, answer: dict):
        if to_user in self.active_connections:
            await self.active_connections[to_user].send_json({
                "type": "call_answer",
                "from": from_user,
                "call_id": call_id,
                "answer": answer
            })

    async def send_ice_candidate(self, from_user: str, to_user: str, call_id: str, candidate: dict):
        if to_user in self.active_connections:
            await self.active_connections[to_user].send_json({
                "type": "ice_candidate",
                "from": from_user,
                "call_id": call_id,
                "candidate": candidate
            })

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            print(f"User {user_id} disconnected. Active connections: {list(self.active_connections.keys())}")


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
            print(f"Received data from {user_id}: {data}")

            if data["type"] == "message":
                success = await manager.send_message(
                    sender=user_id,
                    receiver=data["to"],
                    message=data["message"]
                )
                if success:
                    await websocket.send_json({
                        "type": "message_status",
                        "status": "delivered",
                        "to": data["to"],
                        "timestamp": str(datetime.now())
                    })

            elif data["type"] == "call_offer":
                call_id = f"{user_id}_{data['to']}"
                await manager.send_call_offer(
                    from_user=user_id,
                    to_user=data["to"],
                    call_id=call_id,
                    offer=data.get("offer")
                )

            elif data["type"] == "call_answer":
                await manager.send_call_answer(
                    from_user=user_id,
                    to_user=data["to"],
                    call_id=data["call_id"],
                    answer=data["answer"]
                )

            elif data["type"] == "ice_candidate":
                await manager.send_ice_candidate(
                    from_user=user_id,
                    to_user=data["to"],
                    call_id=data["call_id"],
                    candidate=data["candidate"]
                )

    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except json.JSONDecodeError:
        print(f"Invalid JSON received from {user_id}")
    except Exception as e:
        print(f"Error with {user_id}: {str(e)}")
        manager.disconnect(user_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
