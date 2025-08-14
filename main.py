from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import logging
import sqlite3
from typing import Optional
import hashlib

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Инициализация базы данных
def init_db():
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
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
                FOREIGN KEY(sender_id) REFERENCES users(id),
                FOREIGN KEY(receiver_id) REFERENCES users(id)
            )
        ''')
        conn.commit()

init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.pending_calls = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Active: {list(self.active_connections.keys())}")

    async def send_json(self, receiver_id: str, message: dict):
        if receiver_id in self.active_connections:
            try:
                await self.active_connections[receiver_id].send_json(message)
                return True
            except Exception as e:
                logger.error(f"Error sending to {receiver_id}: {str(e)}")
                del self.active_connections[receiver_id]
                return False
        logger.warning(f"Receiver {receiver_id} not connected")
        return False

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")

manager = ConnectionManager()

# Хэширование пароля
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# Проверка пользователя
def authenticate_user(username: str, password: str) -> Optional[dict]:
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, username, password FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        
        if user and user[2] == hash_password(password):
            return {"id": user[0], "username": user[1]}
        return None

# Регистрация пользователя
def register_user(username: str, password: str) -> Optional[dict]:
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return None
        
    hashed_password = hash_password(password)
    
    try:
        with sqlite3.connect('messenger.db') as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', 
                          (username, hashed_password))
            conn.commit()
            return {"id": cursor.lastrowid, "username": username}
    except sqlite3.IntegrityError:
        return None

# Получение контактов пользователя
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

# Получение истории сообщений
def get_message_history(user_id: int, contact_id: int):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sender_id, message, timestamp 
            FROM messages 
            WHERE (sender_id = ? AND receiver_id = ?) 
               OR (sender_id = ? AND receiver_id = ?)
            ORDER BY timestamp
        ''', (user_id, contact_id, contact_id, user_id))
        return cursor.fetchall()

# Сохранение сообщения
def save_message(sender_id: int, receiver_id: int, message: str):
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (sender_id, receiver_id, message)
            VALUES (?, ?, ?)
        ''', (sender_id, receiver_id, message))
        conn.commit()

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
                                        {"request": request, "error": "Invalid username or password"})
    
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
                                        {"request": request, "error": "Passwords don't match"})
    
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return templates.TemplateResponse("register.html", 
                                        {"request": request, 
                                         "error": "Username must start with # and be 6-16 characters long"})
    
    user = register_user(username, password)
    if not user:
        return templates.TemplateResponse("register.html", 
                                        {"request": request, "error": "Username already taken"})
    
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
    contact_username = data.get("contact_username")
    
    if not user_id or not contact_username:
        return {"success": False, "message": "Invalid data"}
    
    if not contact_username.startswith('#') or len(contact_username) < 6 or len(contact_username) > 16:
        return {"success": False, "message": "Invalid username format"}
    
    with sqlite3.connect('messenger.db') as conn:
        cursor = conn.cursor()
        
        # Проверяем существование контакта
        cursor.execute('SELECT id, username FROM users WHERE username = ?', (contact_username,))
        contact = cursor.fetchone()
        if not contact:
            return {"success": False, "message": "User not found"}
        
        contact_id, contact_username = contact[0], contact[1]
        
        # Проверяем, не пытаемся ли добавить себя
        if contact_id == user_id:
            return {"success": False, "message": "You can't add yourself"}
        
        # Проверяем, есть ли уже такой контакт
        cursor.execute('''
            SELECT id FROM contacts 
            WHERE user_id = ? AND contact_id = ?
        ''', (user_id, contact_id))
        if cursor.fetchone():
            return {"success": False, "message": "Contact already exists"}
        
        # Добавляем контакт
        try:
            cursor.execute('''
                INSERT INTO contacts (user_id, contact_id) 
                VALUES (?, ?)
            ''', (user_id, contact_id))
            conn.commit()
            return {"success": True, "contact_id": contact_id, "contact_username": contact_username}
        except sqlite3.Error as e:
            return {"success": False, "message": str(e)}

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
                # Сохраняем сообщение в БД
                save_message(int(user_id), int(data["to"]), data["message"])
                
                await manager.send_json(data["to"], {
                    "type": "message",
                    "from": user_id,
                    "message": data["message"],
                    "timestamp": str(datetime.now())
                })

            elif data["type"] == "call_request":
                call_id = f"{user_id}_{data['to']}_{str(uuid.uuid4())[:8]}"
                manager.pending_calls[call_id] = {
                    "from": user_id,
                    "to": data["to"],
                    "status": "waiting"
                }
                await manager.send_json(data["to"], {
                    "type": "call_incoming",
                    "from": user_id,
                    "call_id": call_id,
                    "is_audio_only": True
                })
                await websocket.send_json({
                    "type": "call_waiting",
                    "call_id": call_id,
                    "to": data["to"]
                })

            elif data["type"] == "call_accept":
                call_id = data["call_id"]
                if call_id in manager.pending_calls:
                    await manager.send_json(manager.pending_calls[call_id]["from"], {
                        "type": "call_accepted",
                        "call_id": call_id,
                        "by": user_id
                    })
                    del manager.pending_calls[call_id]

            elif data["type"] == "call_reject":
                call_id = data["call_id"]
                if call_id in manager.pending_calls:
                    await manager.send_json(manager.pending_calls[call_id]["from"], {
                        "type": "call_rejected",
                        "call_id": call_id,
                        "by": user_id
                    })
                    del manager.pending_calls[call_id]

            elif data["type"] == "webrtc_offer":
                await manager.send_json(data["to"], {
                    "type": "webrtc_offer",
                    "from": user_id,
                    "call_id": data["call_id"],
                    "offer": data["offer"],
                    "is_audio_only": True
                })

            elif data["type"] == "webrtc_answer":
                await manager.send_json(data["to"], {
                    "type": "webrtc_answer",
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
        logger.error(f"Error with {user_id}: {str(e)}")
        manager.disconnect(user_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
