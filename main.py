from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import uuid

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.call_offers = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    async def send_message(self, sender: str, receiver: str, message: str):
        if receiver in self.active_connections:
            await self.active_connections[receiver].send_json({
                "type": "message",
                "from": sender,
                "message": message
            })

    async def send_call_offer(self, from_user: str, to_user: str, call_id: str):
        if to_user in self.active_connections:
            await self.active_connections[to_user].send_json({
                "type": "call_offer",
                "from": from_user,
                "call_id": call_id
            })

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
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

            if data["type"] == "message":
                await manager.send_message(
                    sender=user_id,
                    receiver=data["to"],
                    message=data["message"]
                )

            elif data["type"] == "call_offer":
                call_id = f"{user_id}_{data['to']}"
                await manager.send_call_offer(
                    from_user=user_id,
                    to_user=data["to"],
                    call_id=call_id
                )

    except WebSocketDisconnect:
        manager.disconnect(user_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
