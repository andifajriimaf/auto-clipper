"""Konfigurasi aplikasi: load/save config.json (API key, model, dsb)."""
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

_defaults = {
    "zai_api_key": "",
    "zai_model": "glm-4.5-flash",
    "zai_base_url": "https://api.z.ai/api/paas/v4/chat/completions",
    "whisper_model": "base",          # tiny | base | small | medium
    "output_dir": str(BASE_DIR / "output"),
    "cookies_browser": "",            # "" | edge | chrome | firefox | brave | opera
    "cookies_file": "",               # path ke cookies.txt hasil ekspor ekstensi
    "render_encoder": "cpu",          # cpu (stabil) | auto | qsv (GPU, bisa bikin layar kedap)
    "channels": [],                   # daftar channel yang dipantau
    "channel_auto": False,            # cek otomatis video baru tiap 30 menit
}

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        data = dict(_defaults)
        if CONFIG_PATH.exists():
            try:
                data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        # env var override, praktis tanpa buka settings
        env_key = os.environ.get("ZAI_API_KEY")
        if env_key:
            data["zai_api_key"] = env_key
        _cache = data
    return _cache


def get(key: str, default=None):
    with _lock:
        return _load().get(key, default)


def update(**kwargs) -> None:
    global _cache
    with _lock:
        data = _load()
        for k, v in kwargs.items():
            if k in _defaults and v is not None:
                data[k] = v
        CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        _cache = data


def masked() -> dict:
    """Config untuk dikirim ke UI — API key hanya ditampilkan sebagian."""
    data = dict(_load())
    key = data.get("zai_api_key") or ""
    if len(key) > 8:
        data["zai_api_key"] = key[:4] + "*" * (len(key) - 8) + key[-4:]
    return data
