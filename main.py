from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import logging
import sqlite3
import hashlib
from typing import Optional, Dict

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.active_calls: Dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Active connections: {len(self.active_connections)}")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception as e:
                logger.error(f"Error sending to {user_id}: {str(e)}")
                self.disconnect(user_id)


manager = ConnectionManager()


def get_client_identifier(request: Request):
    """Упрощенная идентификация устройства по IP"""
    client_ip = request.client.host or "127.0.0.1"  # fallback для локального тестирования
    return hashlib.sha256(client_ip.encode()).hexdigest()


def check_device_limit(identifier: str):
    """Проверяет ограничение на количество аккаунтов с устройства"""
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users WHERE device_id = ?', (identifier,))
        return cursor.fetchone()[0] >= 1


def init_db():
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                device_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(contact_id) REFERENCES users(id),
                UNIQUE(user_id, contact_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE,
                FOREIGN KEY(sender_id) REFERENCES users(id),
                FOREIGN KEY(receiver_id) REFERENCES users(id)
            )
        ''')
        conn.commit()


init_db()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def authenticate_user(username: str, password: str) -> Optional[dict]:
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, username, password FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        if user and user[2] == hash_password(password):
            return {"id": user[0], "username": user[1]}
        return None


def register_user(username: str, password: str, device_id: str) -> Optional[dict]:
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return {"error": "Имя пользователя должно начинаться с # и содержать 6-16 символов"}

    if check_device_limit(device_id):
        return {"error": "Это устройство уже создавало аккаунт"}

    hashed_password = hash_password(password)
    try:
        with sqlite3.connect('messenger.db') as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (username, password, device_id) VALUES (?, ?, ?)',
                           (username, hashed_password, device_id))
            conn.commit()
            return {"id": cursor.lastrowid, "username": username}
    except sqlite3.IntegrityError:
        return {"error": "Имя пользователя уже занято"}


def get_user_contacts(user_id: int):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.id, u.username 
            FROM contacts c
            JOIN users u ON c.contact_id = u.id
            WHERE c.user_id = ?
        ''', (user_id,))
        return [{"id": row[0], "username": row[1]} for row in cursor.fetchall()]


def get_message_history(user_id: int, contact_id: int):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.sender_id, u.username as sender_username, m.message, m.timestamp 
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE (m.sender_id = ? AND m.receiver_id = ?) 
               OR (m.sender_id = ? AND m.receiver_id = ?)
            ORDER BY m.timestamp
        ''', (user_id, contact_id, contact_id, user_id))
        return cursor.fetchall()


def save_message(sender_id: int, receiver_id: int, message: str):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (sender_id, receiver_id, message)
            VALUES (?, ?, ?)
        ''', (sender_id, receiver_id, message))
        conn.commit()


def get_username(user_id: int) -> str:
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else "unknown"


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username, password)
    if not user:
        return templates.TemplateResponse("login.html",
                                          {"request": request, "error": "Неверное имя пользователя или пароль"})

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']))
    response.set_cookie(key="username", value=user['username'])
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register(request: Request,
                   username: str = Form(...),
                   password: str = Form(...),
                   confirm_password: str = Form(...)):
    if password != confirm_password:
        return templates.TemplateResponse("register.html",
                                          {"request": request, "error": "Пароли не совпадают"})

    device_id = get_client_identifier(request)
    user = register_user(username, password, device_id)

    if not user:
        return templates.TemplateResponse("register.html",
                                          {"request": request, "error": "Имя пользователя уже занято"})

    if "error" in user:
        return templates.TemplateResponse("register.html",
                                          {"request": request, "error": user["error"]})

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']))
    response.set_cookie(key="username", value=user['username'])
    return response


@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    username = request.cookies.get("username")
    if not username:
        return RedirectResponse(url="/login")

    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE id = ?', (int(user_id),))
        if not cursor.fetchone():
            return RedirectResponse(url="/login")

    contacts = get_user_contacts(int(user_id))
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user_id,
        "username": username,
        "contacts": contacts
    })


@app.post("/add-contact")
async def add_contact(request: Request):
    data = await request.json()
    user_id = int(data.get("user_id"))
    contact_username = data.get("contact_username").strip().lower()

    if not user_id or not contact_username:
        return {"success": False, "message": "Необходимо указать ID пользователя и имя контакта"}

    if not contact_username.startswith('#') or len(contact_username) < 6 or len(contact_username) > 16:
        return {"success": False, "message": "Имя пользователя должно начинаться с # и содержать 6-16 символов"}

    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE id = ?', (user_id,))
        current_user = cursor.fetchone()

        if not current_user:
            return {"success": False, "message": "Текущий пользователь не найден"}

        current_username = current_user[0].lower()

        if contact_username == current_username:
            return {"success": False, "message": "Вы не можете добавить самого себя"}

        cursor.execute('SELECT id, username FROM users WHERE LOWER(username) = ?', (contact_username,))
        contact = cursor.fetchone()

        if not contact:
            return {"success": False, "message": "Пользователь не найден"}

        contact_id, contact_username = contact[0], contact[1]

        cursor.execute('''
            SELECT id FROM contacts 
            WHERE user_id = ? AND contact_id = ?
        ''', (user_id, contact_id))

        if cursor.fetchone():
            return {"success": False, "message": "Этот пользователь уже есть в ваших контактах"}

        try:
            cursor.execute('''
                INSERT INTO contacts (user_id, contact_id) 
                VALUES (?, ?)
            ''', (user_id, contact_id))
            conn.commit()

            return {
                "success": True,
                "contact_id": contact_id,
                "contact_username": contact_username,
                "message": "Контакт успешно добавлен"
            }
        except sqlite3.Error as e:
            return {"success": False, "message": f"Ошибка базы данных: {str(e)}"}


@app.post("/remove-contact")
async def remove_contact(request: Request):
    data = await request.json()
    user_id = int(data.get("user_id"))
    contact_id = int(data.get("contact_id"))

    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM contacts 
            WHERE user_id = ? AND contact_id = ?
        ''', (user_id, contact_id))
        conn.commit()

        return {"success": True, "message": "Контакт успешно удален"}


@app.get("/get-messages")
async def get_messages(user_id: int, contact_id: int):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.sender_id, u.username as sender_username, m.message, m.timestamp 
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE (m.sender_id = ? AND m.receiver_id = ?) 
               OR (m.sender_id = ? AND m.receiver_id = ?)
            ORDER BY m.timestamp
        ''', (user_id, contact_id, contact_id, user_id))

        messages = []
        for row in cursor.fetchall():
            messages.append({
                "sender_id": row[0],
                "sender_username": row[1],
                "message": row[2],
                "timestamp": row[3]
            })

        return messages


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user_id")
    response.delete_cookie("username")
    return response


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            logger.info(f"Received from {user_id}: {data}")

            if data["type"] == "message":
                receiver_id = data["to"]
                message_text = data["message"]

                save_message(int(user_id), int(receiver_id), message_text)

                with sqlite3.connect('messenger.db') as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT 1 FROM contacts 
                        WHERE user_id = ? AND contact_id = ?
                    ''', (receiver_id, user_id))
                    is_mutual = cursor.fetchone() is not None

                await manager.send_personal_message({
                    "type": "message",
                    "from": user_id,
                    "message": message_text,
                    "timestamp": str(datetime.now()),
                    "is_mutual": is_mutual
                }, receiver_id)

                if not is_mutual:
                    await manager.send_personal_message({
                        "type": "notification",
                        "from": user_id,
                        "message": f"Новое сообщение от #{get_username(int(user_id))}",
                        "timestamp": str(datetime.now())
                    }, receiver_id)

            elif data["type"] == "call_initiate":
                call_id = f"{user_id}_{data['to']}_{str(uuid.uuid4())[:8]}"
                await manager.send_personal_message({
                    "type": "call_incoming",
                    "from": user_id,
                    "call_id": call_id,
                    "is_audio_only": True
                }, data["to"])

            elif data["type"] == "call_accept":
                await manager.send_personal_message({
                    "type": "call_accepted",
                    "from": user_id,
                    "call_id": data["call_id"]
                }, data["to"])

            elif data["type"] == "call_reject":
                await manager.send_personal_message({
                    "type": "call_rejected",
                    "from": user_id,
                    "call_id": data["call_id"]
                }, data["to"])

            elif data["type"] == "webrtc_offer":
                await manager.send_personal_message({
                    "type": "webrtc_offer",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "offer": data["offer"],
                    "is_audio_only": True
                }, data["to"])

            elif data["type"] == "webrtc_answer":
                await manager.send_personal_message({
                    "type": "webrtc_answer",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "answer": data["answer"]
                }, data["to"])

            elif data["type"] == "ice_candidate":
                await manager.send_personal_message({
                    "type": "ice_candidate",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "candidate": data["candidate"]
                }, data["to"])

            elif data["type"] == "call_end":
                await manager.send_personal_message({
                    "type": "call_ended",
                    "from": user_id,
                    "call_id": data["call_id"]
                }, data["to"])

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        logger.info(f"User {user_id} disconnected")
    except Exception as e:
        logger.error(f"Error with {user_id}: {str(e)}")
        manager.disconnect(user_id)


@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call_page(request: Request, call_id: str):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")

    return templates.TemplateResponse("call.html", {
        "request": request,
        "call_id": call_id,
        "user_id": user_id
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
