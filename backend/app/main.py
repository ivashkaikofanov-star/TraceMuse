from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .db import init_db, database_path

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="TraceMuse API",
    description="Локальный API предварительного аудита цифровых материалов.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "TraceMuse API", "database": str(database_path())}

@app.get("/api")
def api_info():
    return {
        "name": "TraceMuse",
        "version": "0.1.0",
        "stage": 1,
        "notice": "Стартовый каркас. Результаты правового и лицензионного анализа пока не реализованы."
    }
