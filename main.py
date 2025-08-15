from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import logging
import hashlib
import psycopg2
from typing import Optional, Dict, List

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Подключение к PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host="dpg-d2fi9r3e5dus73apkggg-a.oregon-postgres.render.com",  # Ваш реальный hostname
            database="aaa_30ug",
            user="aaa_30ug_user",
            password="roIwRVLkjaTxCEyReYZmdMZBb5z8y0v3",
            port=5432,
            sslmode='require'
        )
        return conn
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        raise

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_calls: Dict[str, dict] = {}
        self.user_notifications: Dict[str, List[dict]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Active: {list(self.active_connections.keys())}")

        if user_id in self.user_notifications:
            for notification in self.user_notifications[user_id]:
                await self.send_json(user_id, notification)
            self.user_notifications[user_id] = []

    async def send_json(self, receiver_id: str, message: dict):
        if receiver_id in self.active_connections:
            try:
                await self.active_connections[receiver_id].send_json(message)
                return True
            except Exception as e:
                logger.error(f"Error sending to {receiver_id}: {str(e)}")
                del self.active_connections[receiver_id]
                return False
        else:
            if receiver_id not in self.user_notifications:
                self.user_notifications[receiver_id] = []
            self.user_notifications[receiver_id].append(message)
            logger.info(f"Notification queued for {receiver_id}")
            return False

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")

manager = ConnectionManager()

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(contact_id) REFERENCES users(id),
                UNIQUE(user_id, contact_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
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
    finally:
        conn.close()

init_db()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def authenticate_user(username: str, password: str) -> Optional[dict]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT id, username, password FROM users WHERE username = %s', (username,))
        user = cursor.fetchone()

        if user and user[2] == hash_password(password):
            return {"id": user[0], "username": user[1]}
        return None
    finally:
        conn.close()

def register_user(username: str, password: str) -> Optional[dict]:
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return None

    hashed_password = hash_password(password)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id',
            (username, hashed_password)
        )
        user_id = cursor.fetchone()[0]
        conn.commit()
        return {"id": user_id, "username": username}
    except psycopg2.IntegrityError:
        return None
    finally:
        conn.close()

def get_user_contacts(user_id: int):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.id, u.username 
            FROM contacts c
            JOIN users u ON c.contact_id = u.id
            WHERE c.user_id = %s
        ''', (user_id,))
        return [{"id": row[0], "username": row[1]} for row in cursor.fetchall()]
    finally:
        conn.close()

def get_message_history(user_id: int, contact_id: int):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.sender_id, u.username as sender_username, m.message, m.timestamp 
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE (m.sender_id = %s AND m.receiver_id = %s) 
               OR (m.sender_id = %s AND m.receiver_id = %s)
            ORDER BY m.timestamp
        ''', (user_id, contact_id, contact_id, user_id))
        return cursor.fetchall()
    finally:
        conn.close()

def save_message(sender_id: int, receiver_id: int, message: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (sender_id, receiver_id, message)
            VALUES (%s, %s, %s)
        ''', (sender_id, receiver_id, message))
        conn.commit()
    finally:
        conn.close()

def get_username(user_id: str) -> str:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE id = %s', (int(user_id),))
        result = cursor.fetchone()
        return result[0] if result else "unknown"
    finally:
        conn.close()

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
    # Проверка аутентификации
    if not (username := request.cookies.get("username")):
        return RedirectResponse(url="/login")

    # Валидация user_id
    try:
        user_id_int = int(user_id)
    except ValueError:
        return RedirectResponse(url="/login")

    # Проверка существования пользователя
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT id FROM users WHERE id = %s', (user_id_int,))
            if not cursor.fetchone():
                return RedirectResponse(url="/login")
    finally:
        conn.close()

    # Получение контактов с безопасной обработкой
    try:
        contacts = get_user_contacts(user_id_int) or []  # Гарантированный список
    except Exception as e:
        logger.error(f"Error getting contacts for {user_id_int}: {str(e)}")
        contacts = []

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_id": user_id,
            "username": username,
            "contacts": contacts  # Гарантированно список
        }
    )

@app.post("/add-contact")
async def add_contact(request: Request):
    data = await request.json()
    user_id = int(data.get("user_id"))
    contact_username = data.get("contact_username").strip().lower()

    if not user_id or not contact_username:
        return {"success": False, "message": "Необходимо указать ID пользователя и имя контакта"}

    if not contact_username.startswith('#') or len(contact_username) < 6 or len(contact_username) > 16:
        return {"success": False, "message": "Имя пользователя должно начинаться с # и содержать 6-16 символов"}

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT username FROM users WHERE id = %s', (user_id,))
        current_user = cursor.fetchone()

        if not current_user:
            return {"success": False, "message": "Текущий пользователь не найден"}

        current_username = current_user[0].lower()

        if contact_username == current_username:
            return {"success": False, "message": "Вы не можете добавить самого себя"}

        cursor.execute('SELECT id, username FROM users WHERE LOWER(username) = %s', (contact_username,))
        contact = cursor.fetchone()

        if not contact:
            return {"success": False, "message": "Пользователь не найден"}

        contact_id, contact_username = contact[0], contact[1]

        cursor.execute('''
            SELECT id FROM contacts 
            WHERE user_id = %s AND contact_id = %s
        ''', (user_id, contact_id))

        if cursor.fetchone():
            return {"success": False, "message": "Этот пользователь уже есть в ваших контактах"}

        cursor.execute('''
            INSERT INTO contacts (user_id, contact_id) 
            VALUES (%s, %s) RETURNING id
        ''', (user_id, contact_id))
        conn.commit()

        return {
            "success": True,
            "contact_id": contact_id,
            "contact_username": contact_username,
            "message": "Контакт успешно добавлен"
        }
    except psycopg2.Error as e:
        return {"success": False, "message": f"Ошибка базы данных: {str(e)}"}
    finally:
        conn.close()

@app.post("/remove-contact")
async def remove_contact(request: Request):
    data = await request.json()
    user_id = int(data.get("user_id"))
    contact_id = int(data.get("contact_id"))

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM contacts 
            WHERE user_id = %s AND contact_id = %s
        ''', (user_id, contact_id))
        conn.commit()
        return {"success": True, "message": "Contact removed successfully"}
    finally:
        conn.close()

@app.get("/get-messages")
async def get_messages(user_id: int, contact_id: int):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.sender_id, u.username as sender_username, m.message, m.timestamp 
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE (m.sender_id = %s AND m.receiver_id = %s) 
               OR (m.sender_id = %s AND m.receiver_id = %s)
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
    finally:
        conn.close()

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

                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT 1 FROM contacts 
                        WHERE user_id = %s AND contact_id = %s
                    ''', (receiver_id, user_id))
                    is_mutual = cursor.fetchone() is not None
                finally:
                    conn.close()

                await manager.send_json(receiver_id, {
                    "type": "message",
                    "from": user_id,
                    "message": message_text,
                    "timestamp": str(datetime.now()),
                    "is_mutual": is_mutual
                })

                if not is_mutual:
                    await manager.send_json(receiver_id, {
                        "type": "notification",
                        "from": user_id,
                        "message": f"New message from #{get_username(user_id)}: {message_text}",
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
