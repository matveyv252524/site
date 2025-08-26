# main.py (обновленная версия)
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import uuid
import logging
import hashlib
import psycopg2
from typing import Optional, Dict, List, AsyncGenerator

# ======================
# НАСТРОЙКА ПРИЛОЖЕНИЯ
# ======================

# Конфигурация логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация базы данных
DB_CONFIG = {
    "host": "dpg-d2gdp2odl3ps73f7jev0-a.oregon-postgres.render.com",
    "database": "database12345",
    "user": "admin",
    "password": "bQH965QR9xrBKCUpUdUv80K7IRjGvEtt",
    "port": "5432",
    "sslmode": "require"
}

# Альтернативные юзернеймы для аккаунтов
ALTERNATE_USERNAMES = {
    "#admin": ["#creator"]
}


# ======================
# КЛАССЫ И ФУНКЦИИ
# ======================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_calls: Dict[str, dict] = {}
        self.user_notifications: Dict[str, List[dict]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Active: {list(self.active_connections.keys())}")

        # Send pending notifications
        if user_id in self.user_notifications:
            for notification in self.user_notifications[user_id]:
                await self.send_json(user_id, notification)
            self.user_notifications[user_id] = []

    async def send_json(self, receiver_id: str, message: dict) -> bool:
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


def get_db_connection():
    """Устанавливает соединение с PostgreSQL"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        logger.info("Database connection established")
        return conn
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Database connection failed")


def init_db():
    """Инициализирует таблицы в базе данных"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Создание таблиц с правильным синтаксисом
        tables = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS alternate_usernames (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                username TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                contact_id INTEGER NOT NULL REFERENCES users(id),
                UNIQUE(user_id, contact_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender_id INTEGER NOT NULL REFERENCES users(id),
                receiver_id INTEGER NOT NULL REFERENCES users(id),
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE
            )
            """
        ]

        for table_ddl in tables:
            try:
                cursor.execute(table_ddl)
                logger.info(f"Successfully executed: {table_ddl.split()[5]}")  # Логируем имя таблицы
            except Exception as e:
                logger.error(f"Error creating table: {str(e)}")
                raise

        # Добавляем альтернативные юзернеймы для администратора
        cursor.execute("SELECT id FROM users WHERE username = '#admin'")
        admin = cursor.fetchone()

        if admin:
            admin_id = admin[0]
            for alt_username in ALTERNATE_USERNAMES.get("#admin", []):
                try:
                    cursor.execute(
                        "INSERT INTO alternate_usernames (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (admin_id, alt_username)
                    )
                except Exception as e:
                    logger.error(f"Error adding alternate username {alt_username}: {str(e)}")

        conn.commit()
        logger.info("All tables initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        raise
    finally:
        if conn is not None:
            conn.close()


def hash_password(password: str) -> str:
    """Хеширует пароль"""
    return hashlib.sha256(password.encode()).hexdigest()


def get_user_by_username(username: str) -> Optional[tuple]:
    """Получает пользователя по юзернейму (основному или альтернативному)"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Сначала проверяем основной юзернейм
        cursor.execute(
            'SELECT id, username, name, password FROM users WHERE username = %s',
            (username,)
        )
        user = cursor.fetchone()

        if user:
            return user

        # Если не найден, проверяем альтернативные юзернеймы
        cursor.execute(
            'SELECT u.id, u.username, u.name, u.password FROM users u '
            'JOIN alternate_usernames a ON u.id = a.user_id WHERE a.username = %s',
            (username,)
        )
        return cursor.fetchone()
    except Exception as e:
        logger.error(f"Error getting user by username: {str(e)}")
        return None
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """Аутентифицирует пользователя"""
    user = get_user_by_username(username)

    if user and user[3] == hash_password(password):
        return {"id": user[0], "username": user[1], "name": user[2]}
    return None


def register_user(username: str, password: str, name: str, description: str = "") -> Optional[dict]:
    """Регистрирует нового пользователя"""
    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return None

    hashed_password = hash_password(password)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (username, password, name, description) '
            'VALUES (%s, %s, %s, %s) RETURNING id',
            (username, hashed_password, name, description)
        )
        user_id = cursor.fetchone()[0]
        conn.commit()
        return {"id": user_id, "username": username, "name": name}
    except psycopg2.IntegrityError:
        return None
    except Exception as e:
        logger.error(f"Error registering user: {str(e)}")
        return None
    finally:
        conn.close()


def get_user_profile(user_id: int) -> Optional[dict]:
    """Получает профиль пользователя"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.username, u.name, u.description,
                   ARRAY_AGG(a.username) AS alternate_usernames
            FROM users u
            LEFT JOIN alternate_usernames a ON u.id = a.user_id
            WHERE u.id = %s
            GROUP BY u.id, u.username, u.name, u.description
        ''', (user_id,))
        result = cursor.fetchone()
        if result:
            return {
                "username": result[0],
                "name": result[1],
                "description": result[2],
                "alternate_usernames": [u for u in (result[3] or []) if u]  # Фильтруем NULL значения
            }
        return None
    except Exception as e:
        logger.error(f"Error getting user profile: {str(e)}")
        return None
    finally:
        conn.close()


def get_user_contacts(user_id: int) -> List[dict]:
    """Получает контакты пользователя"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.id, u.username, u.name 
            FROM contacts c
            JOIN users u ON c.contact_id = u.id
            WHERE c.user_id = %s
        ''', (user_id,))
        return [{"id": row[0], "username": row[1], "name": row[2]} for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error getting contacts: {str(e)}")
        return []
    finally:
        conn.close()


def get_message_history(user_id: int, contact_id: int) -> List[dict]:
    """Получает историю сообщений"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.sender_id, u.username, u.name, m.message, m.timestamp 
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE (m.sender_id = %s AND m.receiver_id = %s) 
               OR (m.sender_id = %s AND m.receiver_id = %s)
            ORDER BY m.timestamp
        ''', (user_id, contact_id, contact_id, user_id))

        return [{
            "sender_id": row[0],
            "sender_username": row[1],
            "sender_name": row[2],
            "message": row[3],
            "timestamp": row[4]
        } for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error getting messages: {str(e)}")
        return []
    finally:
        conn.close()


def save_message(sender_id: int, receiver_id: int, message: str):
    """Сохраняет сообщение в базе данных"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (sender_id, receiver_id, message)
            VALUES (%s, %s, %s)
        ''', (sender_id, receiver_id, message))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving message: {str(e)}")
    finally:
        conn.close()


def get_username(user_id: str) -> str:
    """Получает имя пользователя по ID"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT username FROM users WHERE id = %s', (int(user_id),))
        result = cursor.fetchone()
        return result[0] if result else "unknown"
    except Exception as e:
        logger.error(f"Error getting username: {str(e)}")
        return "unknown"
    finally:
        conn.close()


# ======================
# LIFESPAN HANDLER
# ======================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Обработчик жизненного цикла приложения"""
    # Инициализация при запуске
    logger.info("Starting application initialization...")
    try:
        init_db()
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.critical(f"❌ Critical database initialization error: {str(e)}")
        raise

    yield  # Приложение работает

    # Очистка при завершении
    logger.info("Shutting down application")
    # Здесь можно добавить код для очистки ресурсов


# ======================
# ИНИЦИАЛИЗАЦИЯ FASTAPI
# ======================

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ======================
# МАРШРУТЫ ПРИЛОЖЕНИЯ
# ======================

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
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверное имя пользователя или пароль"}
        )

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']), httponly=True)
    response.set_cookie(key="username", value=user['username'], httponly=True)
    response.set_cookie(key="name", value=user['name'].encode('utf-8').decode('latin-1'), httponly=True)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        confirm_password: str = Form(...),
        name: str = Form(...),
        description: str = Form("")
):
    if password != confirm_password:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Пароли не совпадают"}
        )

    if not username.startswith('#') or len(username) < 6 or len(username) > 16:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "Имя пользователя должно начинаться с # и содержать 6-16 символов"
            }
        )

    user = register_user(username, password, name, description)
    if not user:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Имя пользователя уже занято"}
        )

    response = RedirectResponse(url=f"/chat/{user['id']}", status_code=303)
    response.set_cookie(key="user_id", value=str(user['id']), httponly=True)
    response.set_cookie(key="username", value=user['username'], httponly=True)
    response.set_cookie(key="name", value=user['name'].encode('utf-8').decode('latin-1'), httponly=True)

    return response


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")

    profile = get_user_profile(int(user_id))
    if not profile:
        return RedirectResponse(url="/login")

    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "profile": profile}
    )


@app.post("/update-profile")
async def update_profile(
        request: Request,
        name: str = Form(...),
        description: str = Form("")
):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET name = %s, description = %s
            WHERE id = %s
        ''', (name, description, user_id))
        conn.commit()

        response = RedirectResponse(url="/profile", status_code=303)
        response.set_cookie(key="name", value=name.encode('utf-8').decode('latin-1'), httponly=True)
        return response
    except Exception as e:
        logger.error(f"Error updating profile: {str(e)}")
        return RedirectResponse(url="/profile", status_code=303)
    finally:
        conn.close()


@app.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat(request: Request, user_id: str):
    # Проверка аутентификации
    if not (username := request.cookies.get("username")) or not request.cookies.get("user_id"):
        return RedirectResponse(url="/login")

    # Валидация user_id
    try:
        user_id_int = int(user_id)
        cookie_user_id = int(request.cookies.get("user_id"))
        if user_id_int != cookie_user_id:
            return RedirectResponse(url="/login")
    except ValueError:
        return RedirectResponse(url="/login")

    # Проверка существования пользователя
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT id FROM users WHERE id = %s', (user_id_int,))
            if not cursor.fetchone():
                return RedirectResponse(url="/login")
    except Exception as e:
        logger.error(f"Error verifying user: {str(e)}")
        return RedirectResponse(url="/login")
    finally:
        conn.close()

    # Получение контактов
    try:
        contacts = get_user_contacts(user_id_int)
    except Exception as e:
        logger.error(f"Error getting contacts: {str(e)}")
        contacts = []

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_id": user_id,
            "username": username,
            "name": request.cookies.get("name"),
            "contacts": contacts
        }
    )


@app.post("/add-contact")
async def add_contact(request: Request):
    try:
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

            # Проверка текущего пользователя
            cursor.execute('SELECT username FROM users WHERE id = %s', (user_id,))
            current_user = cursor.fetchone()

            if not current_user:
                return {"success": False, "message": "Текущий пользователь не найден"}

            current_username = current_user[0].lower()

            if contact_username == current_username:
                return {"success": False, "message": "Вы не можете добавить самого себя"}

            # Поиск контакта (основной или альтернативный юзернейм)
            cursor.execute('''
                SELECT u.id, u.username, u.name 
                FROM users u
                LEFT JOIN alternate_usernames a ON u.id = a.user_id
                WHERE LOWER(u.username) = %s OR LOWER(a.username) = %s
                LIMIT 1
            ''', (contact_username, contact_username))
            contact = cursor.fetchone()

            if not contact:
                return {"success": False, "message": "Пользователь не найден"}

            contact_id, contact_username, contact_name = contact[0], contact[1], contact[2]

            # Проверка, есть ли уже контакт
            cursor.execute('''
                SELECT id FROM contacts 
                WHERE user_id = %s AND contact_id = %s
            ''', (user_id, contact_id))

            if cursor.fetchone():
                return {"success": False, "message": "Этот пользователь уже есть в ваших контактах"}

            # Добавление контакта
            cursor.execute('''
                INSERT INTO contacts (user_id, contact_id) 
                VALUES (%s, %s)
            ''', (user_id, contact_id))
            conn.commit()

            return {
                "success": True,
                "contact_id": contact_id,
                "contact_username": contact_username,
                "contact_name": contact_name,
                "message": "Контакт успешно добавлен"
            }
        except psycopg2.Error as e:
            conn.rollback()
            return {"success": False, "message": f"Ошибка базы данных: {str(e)}"}
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error in add_contact: {str(e)}")
        return {"success": False, "message": "Internal server error"}


@app.post("/remove-contact")
async def remove_contact(request: Request):
    try:
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
        except Exception as e:
            conn.rollback()
            return {"success": False, "message": f"Error removing contact: {str(e)}"}
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error in remove_contact: {str(e)}")
        return {"success": False, "message": "Internal server error"}


@app.get("/get-messages")
async def get_messages(user_id: int, contact_id: int):
    try:
        messages = get_message_history(user_id, contact_id)
        return messages
    except Exception as e:
        logger.error(f"Error getting messages: {str(e)}")
        return []


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user_id")
    response.delete_cookie("username")
    response.delete_cookie("name")
    return response


@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call_page(request: Request, call_id: str):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")

    # Проверяем что пользователь участник звонка
    parts = call_id.split('_')
    if user_id not in [parts[0], parts[1]]:
        raise HTTPException(status_code=403, detail="Access denied")

    return templates.TemplateResponse("call.html", {
        "request": request,
        "call_id": call_id,
        "user_id": user_id
    })

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

                # Сохраняем сообщение в базе данных
                save_message(int(user_id), int(receiver_id), message_text)

                # Проверяем взаимность контакта
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

                # Отправляем сообщение получателю
                await manager.send_json(receiver_id, {
                    "type": "message",
                    "from": user_id,
                    "message": message_text,
                    "timestamp": str(datetime.now()),
                    "is_mutual": is_mutual
                })

                # Если контакт не взаимный, отправляем уведомление
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
                    "status": "waiting",
                    "timestamp": datetime.now()
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
                    # Удаляем звонок из ожидающих перед отправкой подтверждения
                    call_data = manager.pending_calls.pop(call_id)
                    await manager.send_json(call_data["from"], {
                        "type": "call_accepted",
                        "call_id": call_id,
                        "by": user_id
                    })

            elif data["type"] == "call_reject":
                call_id = data["call_id"]
                if call_id in manager.pending_calls:
                    call_data = manager.pending_calls.pop(call_id)
                    await manager.send_json(call_data["from"], {
                        "type": "call_rejected",
                        "call_id": call_id,
                        "by": user_id,
                        "reason": "Call rejected by recipient"
                    })

            elif data["type"] == "webrtc_offer":
                call_id = data["call_id"]
                if call_id.split('_')[1] == user_id or call_id.split('_')[0] == user_id:  # Проверяем, что пользователь участник звонка
                    await manager.send_json(data["to"], {
                        "type": "webrtc_offer",
                        "from": user_id,
                        "call_id": call_id,
                        "offer": data["offer"],
                        "is_audio_only": data.get("is_audio_only", True)
                    })

            elif data["type"] == "webrtc_answer":
                call_id = data["call_id"]
                if call_id.split('_')[1] == user_id or call_id.split('_')[0] == user_id:
                    await manager.send_json(data["to"], {
                        "type": "webrtc_answer",
                        "from": user_id,
                        "call_id": call_id,
                        "answer": data["answer"]
                    })

            elif data["type"] == "ice_candidate":
                call_id = data["call_id"]
                if call_id.split('_')[1] == user_id or call_id.split('_')[0] == user_id:
                    await manager.send_json(data["to"], {
                        "type": "ice_candidate",
                        "from": user_id,
                        "call_id": call_id,
                        "candidate": data["candidate"]
                    })

            elif data["type"] == "call_end":
                call_id = data["call_id"]
                other_user = call_id.split('_')[0] if call_id.split('_')[1] == user_id else call_id.split('_')[1]
                await manager.send_json(other_user, {
                    "type": "call_ended",
                    "call_id": call_id,
                    "by": user_id
                })
                # Очищаем pending calls
                if call_id in manager.pending_calls:
                    manager.pending_calls.pop(call_id)

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        logger.info(f"User {user_id} disconnected")
        # Отменяем все ожидающие звонки от этого пользователя
        for call_id, call_data in list(manager.pending_calls.items()):
            if call_data["from"] == user_id:
                await manager.send_json(call_data["to"], {
                    "type": "call_rejected",
                    "call_id": call_id,
                    "by": user_id,
                    "reason": "Caller disconnected"
                })
                manager.pending_calls.pop(call_id)
    except Exception as e:
        logger.error(f"Error with {user_id}: {str(e)}")
        manager.disconnect(user_id)
        try:
            await websocket.close()
        except:
            pass


# ======================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ======================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
