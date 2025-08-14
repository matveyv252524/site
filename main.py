from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import os
import uuid
import logging
import asyncpg
from typing import Optional, AsyncIterator, List, Dict
import hashlib
from contextlib import asynccontextmanager

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация базы данных
DATABASE_URL = os.environ.get('DATABASE_URL', "postgresql://user:password@localhost/dbname")
pool: Optional[asyncpg.pool.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global pool
    # Инициализация пула подключений
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        logger.info("Database connection pool created")
    except Exception as e:
        logger.error(f"Failed to create database pool: {str(e)}")
        raise

    # Создание таблиц, если они не существуют
    try:
        if pool is None:
            raise RuntimeError("Database connection pool is not initialized")

        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(20) UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS contacts (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    contact_id INTEGER NOT NULL REFERENCES users(id),
                    UNIQUE(user_id, contact_id)
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    sender_id INTEGER NOT NULL REFERENCES users(id),
                    receiver_id INTEGER NOT NULL REFERENCES users(id),
                    message TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            logger.info("Database tables initialized")
    except asyncpg.PostgresError as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise

    yield

    # Закрытие пула подключений при остановке
    if pool is not None:
        await pool.close()
        logger.info("Database connection pool closed")


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_calls: Dict[str, Dict] = {}

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Active: {list(self.active_connections.keys())}")

    async def send_json(self, receiver_id: str, message: Dict) -> bool:
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

    def disconnect(self, user_id: str) -> None:
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")


manager = ConnectionManager()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


async def authenticate_user(username: str, password: str) -> Optional[Dict]:
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    try:
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                'SELECT id, username, password FROM users WHERE username = $1',
                username
            )
            if user and user['password'] == hash_password(password):
                return {"id": user['id'], "username": user['username']}
            return None
    except asyncpg.PostgresError as e:
        logger.error(f"Authentication error: {str(e)}")
        return None


async def register_user(username: str, password: str) -> Optional[Dict]:
    if not username or not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return None

    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    hashed_password = hash_password(password)

    try:
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                'INSERT INTO users (username, password) VALUES ($1, $2) RETURNING id',
                username, hashed_password
            )
            return {"id": user_id, "username": username}
    except asyncpg.PostgresError as e:
        logger.error(f"Registration error: {str(e)}")
        return None


async def get_user_contacts(user_id: int) -> List[Dict]:
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    try:
        async with pool.acquire() as conn:
            contacts = await conn.fetch(
                '''
                SELECT u.id, u.username 
                FROM contacts c
                JOIN users u ON c.contact_id = u.id
                WHERE c.user_id = $1
                ''',
                user_id
            )
            return [{"id": row['id'], "username": row['username']} for row in contacts]
    except asyncpg.PostgresError as e:
        logger.error(f"Error getting contacts: {str(e)}")
        return []


async def get_message_history(user_id: int, contact_id: int) -> List[Dict]:
    if not user_id or not contact_id:
        return []

    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    try:
        async with pool.acquire() as conn:
            messages = await conn.fetch(
                '''
                SELECT m.sender_id, u.username as sender_username, m.message, m.timestamp 
                FROM messages m
                JOIN users u ON m.sender_id = u.id
                WHERE (m.sender_id = $1 AND m.receiver_id = $2) 
                   OR (m.sender_id = $2 AND m.receiver_id = $1)
                ORDER BY m.timestamp
                ''',
                user_id, contact_id
            )
            return [
                {
                    "sender_id": msg['sender_id'],
                    "sender_username": msg['sender_username'],
                    "message": msg['message'],
                    "timestamp": msg['timestamp']
                }
                for msg in messages
            ]
    except asyncpg.PostgresError as e:
        logger.error(f"Error getting message history: {str(e)}")
        return []


async def save_message(sender_id: int, receiver_id: int, message: str) -> None:
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO messages (sender_id, receiver_id, message)
                VALUES ($1, $2, $3)
                ''',
                sender_id, receiver_id, message
            )
    except asyncpg.PostgresError as e:
        logger.error(f"Error saving message: {str(e)}")


async def add_contact_db(user_id: int, contact_username: str) -> Dict:
    if pool is None:
        raise RuntimeError("Database connection pool is not initialized")

    try:
        async with pool.acquire() as conn:
            contact = await conn.fetchrow(
                'SELECT id, username FROM users WHERE username = $1',
                contact_username
            )
            if not contact:
                return {"success": False, "message": "User not found"}

            contact_id = contact['id']

            if contact_id == user_id:
                return {"success": False, "message": "You can't add yourself"}

            exists = await conn.fetchval(
                'SELECT 1 FROM contacts WHERE user_id = $1 AND contact_id = $2',
                user_id, contact_id
            )
            if exists:
                return {"success": False, "message": "Contact already exists"}

            await conn.execute(
                'INSERT INTO contacts (user_id, contact_id) VALUES ($1, $2)',
                user_id, contact_id
            )
            return {"success": True, "contact_id": contact_id, "contact_username": contact['username']}
    except asyncpg.PostgresError as e:
        return {"success": False, "message": str(e)}


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = await authenticate_user(username, password)
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

    if not username or not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return templates.TemplateResponse("register.html",
                                          {"request": request,
                                           "error": "Username must start with # and be 6-16 characters long"})

    user = await register_user(username, password)
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
    user_id_cookie = request.cookies.get("user_id")
    if not username or not user_id_cookie or user_id_cookie != user_id:
        return RedirectResponse(url="/login")

    try:
        user_id_int = int(user_id)
    except ValueError:
        return RedirectResponse(url="/login")

    if pool is None:
        return RedirectResponse(url="/login")

    try:
        async with pool.acquire() as conn:
            user_exists = await conn.fetchval(
                'SELECT 1 FROM users WHERE id = $1',
                user_id_int
            )
            if not user_exists:
                return RedirectResponse(url="/login")
    except asyncpg.PostgresError:
        return RedirectResponse(url="/login")

    contacts = await get_user_contacts(user_id_int)
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user_id,
        "username": username,
        "contacts": contacts
    })


@app.post("/add-contact")
async def add_contact(request: Request):
    data = await request.json()
    try:
        user_id = int(data.get("user_id"))
        contact_username = data.get("contact_username")

        if not contact_username:
            return {"success": False, "message": "Invalid data"}

        if not contact_username.startswith('#') or len(contact_username) < 6 or len(contact_username) > 16:
            return {"success": False, "message": "Invalid username format"}

        result = await add_contact_db(user_id, contact_username)
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/get-messages")
async def get_messages(user_id: int, contact_id: int):
    try:
        messages = await get_message_history(user_id, contact_id)
        return messages
    except Exception as e:
        return {"error": str(e)}


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user_id")
    response.delete_cookie("username")
    return response


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    try:
        await manager.connect(websocket, user_id)
    except Exception as e:
        logger.error(f"WebSocket connection error: {str(e)}")
        await websocket.close()
        return

    try:
        while True:
            data = await websocket.receive_json()

            if not isinstance(data, dict) or "type" not in data:
                await websocket.close(code=1003)
                return

            logger.info(f"Received from {user_id}: {data}")

            if data["type"] == "message":
                await save_message(int(user_id), int(data["to"]), data["message"])

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
