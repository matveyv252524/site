from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os
import logging
import psycopg2
import psycopg2.extras
from typing import Optional, Dict, List
import hashlib
from contextlib import contextmanager

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация подключения к Supabase
DATABASE_URL = os.getenv('DATABASE_URL')
conn: Optional[psycopg2.extensions.connection] = None

@contextmanager
def get_db_connection():
    global conn
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {str(e)}")
        raise
    finally:
        if conn is not None:
            conn.close()
            logger.info("Database connection closed")

@contextmanager
def get_db_cursor():
    with get_db_connection() as connection:
        cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            yield cursor
            connection.commit()
        except Exception as e:
            connection.rollback()
            logger.error(f"Database error: {str(e)}")
            raise

def init_db():
    try:
        with get_db_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(20) UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS contacts (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    contact_id INTEGER NOT NULL REFERENCES users(id),
                    UNIQUE(user_id, contact_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    sender_id INTEGER NOT NULL REFERENCES users(id),
                    receiver_id INTEGER NOT NULL REFERENCES users(id),
                    message TEXT NOT NULL,
                    timestamp TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
        logger.info("Database tables initialized")
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Инициализация базы данных при старте
init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_calls: Dict[str, List[dict]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected")

    async def send_json(self, receiver_id: str, message: dict):
        if receiver_id in self.active_connections:
            await self.active_connections[receiver_id].send_json(message)
            return True
        return False

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")

manager = ConnectionManager()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def authenticate_user(username: str, password: str) -> Optional[dict]:
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'SELECT id, username, password FROM users WHERE username = %s',
                (username,)
            )
            user = cursor.fetchone()
            if user and user['password'] == hash_password(password):
                return {"id": user['id'], "username": user['username']}
            return None
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        return None

def register_user(username: str, password: str) -> Optional[dict]:
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return None

    hashed_password = hash_password(password)

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id, username',
                (username, hashed_password))
            user = cursor.fetchone()
            return {"id": user['id'], "username": user['username']}
    except Exception as e:
        logger.error(f"Registration error: {str(e)}")
        return None

def get_user_contacts(user_id: int) -> List[dict]:
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'SELECT u.id, u.username FROM contacts c JOIN users u ON c.contact_id = u.id WHERE c.user_id = %s',
                (user_id,)
            )
            contacts = cursor.fetchall()
            return [dict(contact) for contact in contacts]
    except Exception as e:
        logger.error(f"Error getting contacts: {str(e)}")
        return []

def save_message(sender_id: int, receiver_id: int, message: str):
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'INSERT INTO messages (sender_id, receiver_id, message) VALUES (%s, %s, %s)',
                (sender_id, receiver_id, message)
            )
    except Exception as e:
        logger.error(f"Error saving message: {str(e)}")

def add_contact(user_id: int, contact_username: str) -> dict:
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                'SELECT id, username FROM users WHERE username = %s',
                (contact_username,)
            )
            contact = cursor.fetchone()
            if not contact:
                return {"success": False, "message": "User not found"}

            cursor.execute(
                'INSERT INTO contacts (user_id, contact_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                (user_id, contact['id'])
            )
            return {"success": True, "contact": dict(contact)}
    except Exception as e:
        logger.error(f"Error adding contact: {str(e)}")
        return {"success": False, "message": "Database error"}

def get_chat_history(user_id: int, contact_id: int) -> List[dict]:
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                '''SELECT * FROM messages 
                WHERE (sender_id = %s AND receiver_id = %s) 
                OR (sender_id = %s AND receiver_id = %s)
                ORDER BY timestamp''',
                (user_id, contact_id, contact_id, user_id)
            )
            messages = cursor.fetchall()
            return [dict(message) for message in messages]
    except Exception as e:
        logger.error(f"Error getting chat history: {str(e)}")
        return []

# Маршруты
@app.get("/", response_class=HTMLResponse)
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
                                      {"request": request, "error": "Invalid credentials"})

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']))
    response.set_cookie(key="username", value=user['username'])
    return response

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(request: Request, username: str = Form(...),
               password: str = Form(...), confirm_password: str = Form(...)):
    if password != confirm_password:
        return templates.TemplateResponse("register.html",
                                      {"request": request, "error": "Passwords don't match"})

    user = register_user(username, password)
    if not user:
        return templates.TemplateResponse("register.html",
                                      {"request": request, "error": "Registration failed"})

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']))
    response.set_cookie(key="username", value=user['username'])
    return response

@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    username = request.cookies.get("username")
    if not username:
        return RedirectResponse(url="/login")

    contacts = get_user_contacts(int(user_id))
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user_id,
        "username": username,
        "contacts": contacts
    })

@app.post("/api/add_contact")
async def api_add_contact(request: Request):
    data = await request.json()
    result = add_contact(int(data['user_id']), data['contact_username'])
    return result

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            if data['type'] == 'message':
                save_message(int(data['sender_id']), int(data['receiver_id']), data['message'])
                await manager.send_json(data['receiver_id'], data)
    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
        manager.disconnect(user_id)

@app.get("/api/chat_history/{user_id}/{contact_id}")
async def get_history(user_id: int, contact_id: int):
    history = get_chat_history(user_id, contact_id)
    return {"history": history}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
