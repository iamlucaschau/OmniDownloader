import os
import re
import time
from pathlib import Path
from typing import List
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import uvicorn

app = FastAPI(title="OMNI STUDIO Local Server")

# Chỉ giao diện của chính app được gọi API. Nếu để "*", bất kỳ trang web nào đang mở
# cũng có thể ra lệnh cho server tải video / mở thư mục trên máy.
ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def block_foreign_origins(request: Request, call_next):
    # CORS chỉ chặn việc ĐỌC phản hồi; request POST đơn giản (vd. /api/open-folder) vẫn chạy.
    # Nên chặn hẳn ở đây mọi request mang Origin lạ.
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Origin không được phép"}, status_code=403)
    return await call_next(request)

# Thư mục gốc và thư mục lưu trữ video
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "Downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIE_TXT_PATH = BASE_DIR / "cookies.txt"


# --- MODELS NHẬN DỮ LIỆU TỪ FRONTEND ---
class AnalyzeRequest(BaseModel):
    urls: List[str]
    use_cookies: bool = False
    cookie_source: str = "cookies_txt"

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080p HD"
    include_caption: bool = False
    use_cookies: bool = False
    cookie_source: str = "cookies_txt"


# --- CÁC HÀM TIỆN ÍCH ---
def detect_platform_folder(url: str) -> str:
    u = url.lower()
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "Facebook_Reel"
    if "instagram.com" in u or "instagr.am" in u:
        return "Instagram"
    if "tiktok.com" in u:
        return "TikTok"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "x.com" in u or "twitter.com" in u:
        return "X_Twitter"
    return "Other_Videos"


def format_duration(seconds) -> str:
    if not seconds:
        return "00:00"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_filesize(bytes_val) -> str:
    if not bytes_val:
        return "HD Auto"
    mb = bytes_val / (1024 * 1024)
    return f"{mb:.1f} MB"


def extract_caption_and_hashtags(raw_description: str, fallback_title: str):
    """Tách riêng phần lời dẫn (Caption) và danh sách Hashtag (#...)"""
    text = (raw_description or fallback_title or "").strip()
    if not text:
        return "Không có mô tả", ""

    hashtags_list = re.findall(r"#\w+", text, flags=re.UNICODE)
    unique_hashtags = list(dict.fromkeys(hashtags_list))
    hashtags_str = " ".join(unique_hashtags)

    clean_caption = re.sub(r"#\w+", "", text, flags=re.UNICODE).strip()
    clean_caption = re.sub(r"\n{3,}", "\n\n", clean_caption)

    if not clean_caption:
        clean_caption = fallback_title or "Video không có lời dẫn riêng."

    return clean_caption, hashtags_str


def sanitize_filename(name: str) -> str:
    """Làm sạch tên file để không bị lỗi ký tự đặc biệt trên Windows"""
    cleaned = re.sub(r'[\\/*?:"<>|\n\r\t]', "_", name)
    return cleaned[:80].strip()


def apply_cookie_options(ydl_opts: dict, use_cookies: bool, cookie_source: str) -> dict:
    """Gắn cấu hình Cookie vào yt-dlp từ file cookies.txt hoặc trình duyệt"""
    if not use_cookies:
        return ydl_opts

    if cookie_source == "cookies_txt":
        if COOKIE_TXT_PATH.exists():
            ydl_opts["cookiefile"] = str(COOKIE_TXT_PATH)
        else:
            raise ValueError(
                "Chưa tìm thấy file 'cookies.txt' trong thư mục OmniDownloader! "
                "Hãy xuất file cookies.txt bỏ vào thư mục hoặc chọn trình duyệt khác."
            )
    else:
        # Đọc trực tiếp từ trình duyệt: ('firefox',), ('chrome',), ('edge',), ('brave',)
        ydl_opts["cookiesfrombrowser"] = (cookie_source,)

    return ydl_opts


# --- CÁC ENDPOINTS API ---

@app.get("/")
async def serve_ui():
    """Mở trực tiếp giao diện khi truy cập http://localhost:8000"""
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "Omni Studio Backend đang hoạt động! Hãy đặt file index.html cùng thư mục."}


@app.get("/api/health")
async def health_check():
    """Báo trạng thái Server và kiểm tra xem có sẵn file cookies.txt chưa"""
    return {
        "status": "online",
        "download_dir": str(DOWNLOAD_DIR),
        "has_cookies_txt": COOKIE_TXT_PATH.exists(),
    }


@app.post("/api/open-folder")
async def open_download_folder():
    """Mở trực tiếp cửa sổ Windows Explorer tại thư mục Downloads"""
    try:
        if os.name == "nt":
            os.startfile(str(DOWNLOAD_DIR))
        return {"status": "opened"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/analyze")
async def analyze_links(req: AnalyzeRequest):
    """Phân tích thông tin thực tế của 1 hoặc nhiều link video"""
    results = []
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
    }

    try:
        ydl_opts = apply_cookie_options(ydl_opts, req.use_cookies, req.cookie_source)
    except Exception as cookie_err:
        # Nếu bật cookie nhưng thiếu file, trả về cảnh báo rõ ràng
        for idx, url in enumerate(req.urls):
            results.append({
                "id": f"err_{int(time.time() * 1000)}_{idx}",
                "url": url,
                "title": f"[Thiếu cấu hình Cookie] {str(cookie_err)}",
                "author": "@System",
                "duration": "00:00",
                "size": "N/A",
                "thumbnail": "https://images.unsplash.com/photo-1594322436404-5a0526db4d13?w=400&q=80",
                "caption": str(cookie_err),
                "hashtags": "",
            })
        return {"items": results}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for idx, url in enumerate(req.urls):
            try:
                info = ydl.extract_info(url, download=False)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]

                raw_title = info.get("title") or "Video không tiêu đề"
                raw_desc = info.get("description") or raw_title
                caption, hashtags = extract_caption_and_hashtags(raw_desc, raw_title)
                filesize = info.get("filesize") or info.get("filesize_approx")

                results.append({
                    "id": f"vid_{int(time.time() * 1000)}_{idx}",
                    "url": url,
                    "title": raw_title[:100],
                    "author": f"@{info.get('uploader') or info.get('channel') or info.get('uploader_id') or 'Creator'}",
                    "duration": format_duration(info.get("duration")),
                    "size": format_filesize(filesize),
                    "thumbnail": info.get("thumbnail") or "https://images.unsplash.com/photo-1611162617474-5b21e879e113?w=400&q=80",
                    "caption": caption,
                    "hashtags": hashtags,
                })
            except Exception as e:
                err_msg = str(e)
                if "PermissionError" in err_msg or "Could not copy Chrome cookie" in err_msg:
                    hint_title = "[Trình duyệt đang mở khóa Cookie] Hãy dùng file cookies.txt hoặc đóng trình duyệt!"
                else:
                    hint_title = f"[Cần bật Cookie xác thực hoặc kiểm tra Link] {url[:40]}..."

                results.append({
                    "id": f"err_{int(time.time() * 1000)}_{idx}",
                    "url": url,
                    "title": hint_title,
                    "author": "@Unknown",
                    "duration": "00:00",
                    "size": "N/A",
                    "thumbnail": "https://images.unsplash.com/photo-1594322436404-5a0526db4d13?w=400&q=80",
                    "caption": f"Chi tiết phản hồi: {err_msg[:220]}",
                    "hashtags": "",
                })

    return {"items": results}


@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """Tải video thực tế về ổ cứng + Lưu kèm file Caption nếu tích chọn"""
    platform_folder = detect_platform_folder(req.url)
    target_dir = DOWNLOAD_DIR / platform_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    if "Audio" in req.quality:
        format_str = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]
        ext_template = "%(ext)s"
    elif "4K" in req.quality:
        format_str = "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"
        postprocessors = []
        ext_template = "mp4"
    elif "720p" in req.quality:
        format_str = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best"
        postprocessors = []
        ext_template = "mp4"
    else:
        format_str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[ext=mp4]/best"
        postprocessors = []
        ext_template = "mp4"

    ydl_opts = {
        "format": format_str,
        "outtmpl": str(target_dir / f"[{platform_folder}]_%(uploader,channel,id)s_%(id)s.%(ext)s"),
        "merge_output_format": None if "Audio" in req.quality else ext_template,
        "postprocessors": postprocessors,
        "quiet": False,
        "no_warnings": True,
    }

    try:
        ydl_opts = apply_cookie_options(ydl_opts, req.use_cookies, req.cookie_source)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            video_id = info.get("id") or str(int(time.time()))
            uploader = sanitize_filename(str(info.get("uploader") or info.get("channel") or "Creator"))
            raw_title = info.get("title") or "Video"
            raw_desc = info.get("description") or raw_title

            caption_file_saved = None
            if req.include_caption:
                caption, hashtags = extract_caption_and_hashtags(raw_desc, raw_title)
                txt_filename = f"[{platform_folder}]_{uploader}_{video_id}_CAPTION.txt"
                txt_path = target_dir / txt_filename
                txt_content = (
                    f"TIÊU ĐỀ : {raw_title}\n"
                    f"TÁC GIẢ : @{uploader}\n"
                    f"NỀN TẢNG: {platform_folder}\n"
                    f"LINK GỐC: {req.url}\n"
                    f"CHẤT LƯỢNG: {req.quality}\n"
                    f"{'='*50}\n\n"
                    f"--- CAPTION ---\n{caption}\n\n"
                    f"--- HASHTAGS ---\n{hashtags}\n"
                )
                txt_path.write_text(txt_content, encoding="utf-8")
                caption_file_saved = str(txt_path)

            return {
                "status": "success",
                "folder": str(target_dir),
                "caption_saved": caption_file_saved,
                "title": raw_title,
            }

    except Exception as e:
        err_msg = str(e)
        if "PermissionError" in err_msg or "Could not copy Chrome cookie" in err_msg:
            raise HTTPException(
                status_code=400,
                detail="Trình duyệt đang mở nên khóa Cookie! Hãy chọn 'File cookies.txt' hoặc đóng trình duyệt rồi bấm Tải lại."
            )
        raise HTTPException(status_code=400, detail=f"Lỗi tải video: {err_msg[:200]}")


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("🚀 OMNI MEDIA STUDIO BACKEND ĐANG CHẠY TẠI: http://localhost:8000")
    print(f"📂 Thư mục lưu video : {DOWNLOAD_DIR}")
    print(f"🔑 File cookies.txt  : {'ĐÃ TÌM THẤY ✅' if COOKIE_TXT_PATH.exists() else 'Chưa có (Tùy chọn)'}")
    print("=" * 65 + "\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)