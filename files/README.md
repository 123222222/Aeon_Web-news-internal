# NEXUS — Social Intelligence Dashboard

Hệ thống tổng hợp tin tức đa nền tảng (YouTube, TikTok, Google News) với AI.

## Kiến trúc

```
[Scheduler 5min]
      ↓
[Collectors]  →  Google News RSS (free)
              →  YouTube Data API v3
              →  TikTok via TikWM API (Apify fallback)
      ↓
[AI Processor]  →  OpenAI API (tóm tắt + sentiment + tags)
      ↓
[FastAPI Backend]  →  REST API
      ↓
[HTML Dashboard]  →  Live feed + Stats + Alerts
```

## Cấu trúc thư mục

```
.
├── backend/           # FastAPI + collectors
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/          # Static dashboard
│   ├── index.html
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## Quick Start

### 1. Cài đặt API Keys

Sao chép file mẫu và điền API key của bạn:

```bash
cp .env.example .env
```

File `.env` đặt tại thư mục gốc dự án:

```env
YOUTUBE_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
APIFY_TOKEN=your_token_here
```

**Lấy API Keys:**
- YouTube: https://console.cloud.google.com → Enable YouTube Data API v3
- OpenAI: https://platform.openai.com/
- Apify (TikTok – tuỳ chọn): https://apify.com → Free tier đủ dùng

### 2. Chạy Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate  # tùy chọn
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

> Windows: dùng `.venv\Scripts\activate` thay cho lệnh `source`.

> `python-dotenv` sẽ tự đọc file `.env` (từ thư mục gốc) nên không cần export thủ công.

> TikTok: Hệ thống cố gắng gọi TikWM API trước (không cần token). Nếu muốn dữ liệu chuyên sâu hơn, cung cấp thêm `APIFY_TOKEN` trong `.env` để bật chế độ Apify fallback.

### 3. Mở Dashboard

```bash
cd frontend
python -m http.server 3000
```

Sau đó mở http://localhost:3000. Bạn cũng có thể mở trực tiếp file `frontend/index.html` nếu không cần API (demo mode).

---

## Docker (Recommended)
```bash
# Copy env file (nếu chưa có)
cp .env.example .env

# Start everything
docker compose up --build -d
```

Dashboard: http://localhost:3000
API Docs: http://localhost:8000/docs

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/news` | Lấy danh sách tin tức |
| POST | `/api/fetch` | Trigger thu thập ngay |
| GET | `/api/keywords` | Danh sách keywords |
| POST | `/api/keywords` | Thêm keyword mới |
| DELETE | `/api/keywords/{kw}` | Xóa keyword |
| GET | `/api/stats` | Thống kê dashboard |

### Query params cho `/api/news`:
- `keyword` — lọc theo keyword
- `platform` — youtube | tiktok | google
- `sentiment` — positive | negative | neutral
- `limit` — số lượng (default 50, max 200)

---

## Production Setup

### Database (PostgreSQL)

Thay `news_store` list trong `main.py` bằng PostgreSQL:

```python
# Cài thêm: pip install asyncpg databases
DATABASE_URL = "postgresql://user:pass@localhost/nexus"
```

### Scheduler tự động (Celery + Redis)

```python
# Cài thêm: pip install celery redis
# Chạy worker: celery -A tasks worker -l info
# Chạy beat: celery -A tasks beat -l info
```

### Deploy lên VPS (Ubuntu)

```bash
# Nginx reverse proxy
sudo apt install nginx
# Config /etc/nginx/sites-available/nexus
# → proxy_pass http://localhost:8000 cho API
# → serve static frontend/index.html

# PM2 cho process management
npm install -g pm2
pm2 start "uvicorn main:app --host 0.0.0.0 --port 8000" --name nexus-api
```

---

## Roadmap

- [ ] PostgreSQL integration
- [ ] Celery scheduled jobs
- [ ] Telegram/Email alerts
- [ ] Facebook Graph API integration
- [ ] Export to CSV/Excel
- [ ] Multi-user / team workspace
