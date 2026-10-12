"""UI-side configuration (backend base URL)."""

import os

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
