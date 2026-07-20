import os
import sys
import json
import uuid
import io
import threading
import time
import socket
import platform
import hashlib
import subprocess
import httpx
import webbrowser
from typing import List, Optional
# تمت إضافة Form هنا لاستقبال الـ hwid في الرفع
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel
import webview

from network_sync import OrgNetworkManager, NetworkState

# محاولة استيراد مكتبات معالجة البيانات والـ Vector DB
try:
    import chromadb
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    from docx import Document
    import openpyxl
    OFFICE_AVAILABLE = True
except ImportError:
    OFFICE_AVAILABLE = False

# ---------------------------------------------------------
# دالة سحرية لاكتشاف المسار الحقيقي للملفات المدمجة داخل التطبيق المحزوم
# ---------------------------------------------------------
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# ---------------------------------------------------------
# إعدادات عامة
# ---------------------------------------------------------

LICENSE_SERVER_URL = os.environ.get("LICENSE_SERVER_URL", "https://web-production-de335.up.railway.app")
LOCAL_STATE_PATH = os.path.join(os.path.abspath("."), "device_state.json")
SHARED_DB_PORT = 8001
RAG_DB_DIR = "./local_rag_db"
OFFLINE_GRACE_PERIOD_DAYS = 5
RAG_MAX_DISTANCE = float(os.environ.get("RAG_MAX_DISTANCE", "0.8"))
RAG_N_RESULTS = int(os.environ.get("RAG_N_RESULTS", "4"))


# ---------------------------------------------------------
# 1. هوية الجهاز (HWID)
# ---------------------------------------------------------
def get_hardware_id() -> str:
    try:
        system_info = f"{platform.node()}-{platform.system()}-{platform.machine()}-{platform.processor()}"
    except Exception:
        system_info = str(uuid.getnode())
    return hashlib.sha256(system_info.encode()).hexdigest()[:32]

DEVICE_HWID = get_hardware_id()


# ---------------------------------------------------------
# 2. تخزين حالة الترخيص
# ---------------------------------------------------------
def load_local_state() -> dict:
    if not os.path.exists(LOCAL_STATE_PATH):
        return {}
    try:
        with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_local_state(state: dict):
    with open(LOCAL_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def verify_license_with_server(hwid: str) -> dict:
    state = load_local_state()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{LICENSE_SERVER_URL}/api/check-license",
                params={"hwid": hwid},
            )
        if resp.status_code == 200:
            data = resp.json()
            state.update({
                "hwid": hwid,
                "plan": data.get("plan"),
                "active": data.get("valid", False),
                "expires_at": data.get("expires_at"),
                "lan_device_limit": data.get("lan_device_limit"),
                "last_verified_at": time.time(),
            })
            save_local_state(state)
            return {
                "valid": data.get("valid", False),
                "plan": data.get("plan"),
                "expires_at": data.get("expires_at"),
                "lan_device_limit": data.get("lan_device_limit"),
                "trial_available": data.get("trial_available", False),
                "source": "server",
            }
        else:
            return {"valid": False, "plan": None, "source": "server_rejected"}

    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
        if state.get("hwid") == hwid and state.get("active"):
            last_verified = state.get("last_verified_at", 0)
            days_since = (time.time() - last_verified) / 86400
            if days_since <= OFFLINE_GRACE_PERIOD_DAYS:
                return {
                    "valid": True,
                    "plan": state.get("plan"),
                    "expires_at": state.get("expires_at"),
                    "lan_device_limit": state.get("lan_device_limit"),
                    "source": "offline_cache",
                }
        return {"valid": False, "plan": None, "source": "no_cache_or_expired"}


async def verify_paypal_order_with_server(order_id: str, hwid: str, plan: str, device_name: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{LICENSE_SERVER_URL}/api/verify-order",
                json={"orderID": order_id, "hwid": hwid, "plan": plan, "device_name": device_name},
            )
        if resp.status_code == 200:
            data = resp.json()
            state = load_local_state()
            state.update({
                "hwid": hwid,
                "plan": data.get("plan"),
                "active": True,
                "expires_at": data.get("expires_at"),
                "last_verified_at": time.time(),
            })
            save_local_state(state)
            return {"success": True, "plan": data.get("plan"), "expires_at": data.get("expires_at")}
        else:
            try:
                detail = resp.json().get("detail", "فشل التحقق من الدفع")
            except Exception:
                detail = "فشل التحقق من الدفع"
            return {"success": False, "error": detail}
    except Exception as e:
        return {"success": False, "error": f"تعذّر الاتصال بسيرفر الترخيص: {str(e)}"}


async def start_free_trial_with_server(hwid: str, device_name: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{LICENSE_SERVER_URL}/api/trial/start",
                json={"hwid": hwid, "device_name": device_name},
            )
        if resp.status_code == 200:
            data = resp.json()
            state = load_local_state()
            state.update({
                "hwid": hwid,
                "plan": data.get("plan"),
                "active": True,
                "expires_at": data.get("expires_at"),
                "is_trial": True,
                "last_verified_at": time.time(),
            })
            save_local_state(state)
            return {"success": True, "plan": data.get("plan"), "expires_at": data.get("expires_at")}
        else:
            try:
                detail = resp.json().get("detail", "تعذّر بدء التجربة المجانية")
            except Exception:
                detail = "تعذّر بدء التجربة المجانية"
            return {"success": False, "error": detail}
    except Exception as e:
        return {"success": False, "error": f"تعذّر الاتصال بسيرفر الترخيص: {str(e)}"}


network_manager: Optional[OrgNetworkManager] = None
network_state_lock = threading.Lock()
current_network_state = {
    "role": "standalone", "host_hwid": None, "host_ip": None,
    "is_connected": False, "rejection_reason": None,
}

_chroma_server_started = False
_chroma_server_lock = threading.Lock()


def _start_chroma_http_server():
    global _chroma_server_started
    with _chroma_server_lock:
        if _chroma_server_started:
            return
        _chroma_server_started = True

    try:
        from chromadb.config import Settings
        from chromadb.server.fastapi import FastAPI as ChromaFastAPI

        chroma_settings = Settings(
            chroma_server_host="0.0.0.0",
            chroma_server_http_port=SHARED_DB_PORT,
            is_persistent=True,
            persist_directory=RAG_DB_DIR,
        )
        chroma_app_instance = ChromaFastAPI(chroma_settings)
        uvicorn.run(chroma_app_instance.app(), host="0.0.0.0", port=SHARED_DB_PORT, log_level="error")
    except Exception as e:
        print(f"[Host] فشل تشغيل سيرفر chromadb المشترك: {str(e)}")


def on_network_state_change(state: NetworkState):
    with network_state_lock:
        was_host_before = current_network_state["role"] == "host"
        current_network_state.update({
            "role": state.role,
            "host_hwid": state.host_hwid,
            "host_ip": state.host_ip,
            "host_port": state.host_port,
            "is_connected": state.is_connected,
            "rejection_reason": state.rejection_reason,
        })

    if state.role == "host" and not was_host_before:
        threading.Thread(target=_start_chroma_http_server, daemon=True).start()


def start_network_sync():
    global network_manager
    if network_manager:
        return
    device_name = platform.node() or "جهاز غير مسمّى"
    network_manager = OrgNetworkManager(
        my_hwid=DEVICE_HWID,
        license_server_url=LICENSE_SERVER_URL,
        local_port=SHARED_DB_PORT,
        device_name=device_name,
    )
    threading.Thread(
        target=network_manager.start,
        kwargs={"on_state_change": on_network_state_change},
        daemon=True,
    ).start()


def get_chroma_client():
    with network_state_lock:
        role = current_network_state["role"]
        host_ip = current_network_state["host_ip"]
        host_port = current_network_state.get("host_port", SHARED_DB_PORT)

    if role == "client" and host_ip:
        return chromadb.HttpClient(host=host_ip, port=host_port)
    elif role == "host":
        return chromadb.HttpClient(host="127.0.0.1", port=host_port)
    else:
        return chromadb.PersistentClient(path=RAG_DB_DIR)


OLLAMA_EMBEDDING_MODEL = os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")


class OllamaEmbeddingFunction:
    def __init__(self, model_name: str = OLLAMA_EMBEDDING_MODEL, base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.base_url = base_url

    def name(self) -> str:
        return f"ollama-{self.model_name}"

    def __call__(self, input):
        texts = input if isinstance(input, list) else [input]
        embeddings = []
        with httpx.Client(timeout=60.0) as client:
            for text in texts:
                resp = client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model_name, "prompt": text},
                )
                resp.raise_for_status()
                embeddings.append(resp.json()["embedding"])
        return embeddings


_ollama_embedding_function = OllamaEmbeddingFunction()


# ---------------------------------------------------------
# 4. إعداد تطبيق FastAPI
# ---------------------------------------------------------
app = FastAPI()

templates_dir = get_resource_path("templates")
static_dir = get_resource_path("static")

if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)


# ---------------------------------------------------------
# 5. نظام التتبع الحقيقي
# ---------------------------------------------------------
download_status = {
    "flash": {"status": "idle", "progress": 0},
    "plus": {"status": "idle", "progress": 0},
    "pro": {"status": "idle", "progress": 0},
}

OLLAMA_MODELS = {
    "flash": "qwen2.5:1.5b",
    "plus": "qwen2.5:7b",
    "pro": "qwen2.5:32b",
}


async def real_pull_model_task(model_key: str, actual_name: str):
    download_status[model_key] = {"status": "downloading", "progress": 0}
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream('POST', 'http://localhost:11434/api/pull', json={"name": actual_name, "stream": True}) as response:
                async for line in response.aiter_lines():
                    if line:
                        data = json.loads(line)
                        if "total" in data and "completed" in data and data["total"] > 0:
                            progress = int((data["completed"] / data["total"]) * 100)
                            download_status[model_key]["progress"] = progress
                        elif data.get("status") == "success":
                            download_status[model_key]["progress"] = 100
        download_status[model_key]["status"] = "completed"
    except Exception as e:
        print(f"Error pulling model from Ollama: {str(e)}")
        download_status[model_key] = {"status": "error", "progress": 0}


class ModelRequest(BaseModel):
    model_key: str


@app.post("/api/models/install")
async def install_model_api(request: ModelRequest, bg_tasks: BackgroundTasks):
    model_key = request.model_key
    actual_name = OLLAMA_MODELS.get(model_key, "qwen2.5:1.5b")
    bg_tasks.add_task(real_pull_model_task, model_key, actual_name)
    return {"status": "started", "actual_model": actual_name}


@app.get("/api/models/progress")
async def get_model_progress(model: str):
    return download_status.get(model, {"status": "idle", "progress": 0})


@app.get("/api/models/installed")
async def get_installed_models():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("http://localhost:11434/api/tags")
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[Models] فشل الاستعلام عن نماذج Ollama المثبتة: {str(e)}")
        return {"installed": []}

    installed_actual_names = {m.get("name") for m in data.get("models", [])}
    installed_keys = [
        key for key, actual_name in OLLAMA_MODELS.items()
        if actual_name in installed_actual_names or f"{actual_name}:latest" in installed_actual_names
    ]
    return {"installed": installed_keys}


# ---------------------------------------------------------
# 6. إدارة الروابط الخارجية
# ---------------------------------------------------------
class BrowserRequest(BaseModel):
    url: str


def open_browser_reliably(url: str) -> bool:
    clean_env = os.environ.copy()
    clean_env.pop("LD_LIBRARY_PATH", None)
    original_ld_path = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if original_ld_path:
        clean_env["LD_LIBRARY_PATH"] = original_ld_path

    if platform.system() == "Linux":
        for opener in ("xdg-open", "gio", "gnome-open", "kde-open"):
            try:
                args = [opener, "open", url] if opener == "gio" else [opener, url]
                result = subprocess.run(
                    args, env=clean_env, timeout=5,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

    elif platform.system() == "Darwin":
        try:
            result = subprocess.run(["open", url], env=clean_env, timeout=5,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    elif platform.system() == "Windows":
        try:
            os.startfile(url) 
            return True
        except Exception:
            pass

    try:
        old_ld_path = os.environ.get("LD_LIBRARY_PATH")
        if "LD_LIBRARY_PATH" in clean_env:
            os.environ["LD_LIBRARY_PATH"] = clean_env["LD_LIBRARY_PATH"]
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
        try:
            return bool(webbrowser.open(url))
        finally:
            if old_ld_path is not None:
                os.environ["LD_LIBRARY_PATH"] = old_ld_path
    except Exception:
        return False


CHECKOUT_PAGE_URL = os.environ.get("CHECKOUT_PAGE_URL", f"{LICENSE_SERVER_URL}/checkout")


@app.post("/api/subscribe/open-checkout")
async def open_checkout(plan: str):
    checkout_url = f"{CHECKOUT_PAGE_URL}?hwid={DEVICE_HWID}&plan={plan}"
    success = open_browser_reliably(checkout_url)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="تعذّر فتح المتصفح تلقائياً. افتح هذا الرابط يدوياً: " + checkout_url,
        )
    return {"status": "opened", "url": checkout_url}



@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html", {"hwid": DEVICE_HWID})


# ---------------------------------------------------------
# 7. الاشتراك المباشر
# ---------------------------------------------------------
class VerifyOrderRequest(BaseModel):
    order_id: str
    plan: str


@app.post("/api/subscribe/verify")
async def subscribe_verify_api(data: VerifyOrderRequest):
    device_name = platform.node() or "جهاز غير مسمّى"
    result = await verify_paypal_order_with_server(data.order_id, DEVICE_HWID, data.plan, device_name)
    if result.get("success"):
        start_network_sync()
    return result


@app.post("/api/trial/start")
async def trial_start_api():
    device_name = platform.node() or "جهاز غير مسمّى"
    result = await start_free_trial_with_server(DEVICE_HWID, device_name)
    if result.get("success"):
        start_network_sync()
    return result


@app.get("/api/check-license")
async def check_license_api():
    result = await verify_license_with_server(DEVICE_HWID)
    if result.get("valid") and network_manager is None:
        start_network_sync()
    return result


@app.get("/health")
def health_check():
    with network_state_lock:
        role = current_network_state["role"]
    return {"status": "ok", "hwid": DEVICE_HWID, "role": role}


@app.get("/api/network-status")
def network_status_api():
    with network_state_lock:
        return dict(current_network_state)


class ConnectManualRequest(BaseModel):
    host_ip: str
    host_port: int = SHARED_DB_PORT


@app.post("/api/network-connect-manual")
def network_connect_manual(data: ConnectManualRequest):
    if network_manager is None:
        raise HTTPException(status_code=400, detail="يجب أن يكون لديك اشتراك مفعّل أولاً")
    result = network_manager.connect_manually(data.host_ip, data.host_port)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "تعذّر الاتصال بالجهاز المُدخل، تأكد من العنوان ومن أنه على نفس الشبكة"),
        )
    return {"status": "connected", "host_ip": data.host_ip}


# ---------------------------------------------------------
# 8. معالجة الملفات محلياً (RAG - Word, Excel, PDF)
# ---------------------------------------------------------
@app.post("/api/upload")
async def upload_endpoint(hwid: str = Form(...), files: List[UploadFile] = File(...)):
    # التحقق من أن الطلب وارد فعلاً من الجهاز المرخص بناءً على HWID
    if hwid != DEVICE_HWID:
        raise HTTPException(status_code=403, detail="Invalid Hardware ID")

    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    if not CHROMA_AVAILABLE:
        raise HTTPException(status_code=500, detail="Vector Database not available")

    client = get_chroma_client()
    rag_collection = client.get_or_create_collection(name="company_knowledge", embedding_function=_ollama_embedding_function)

    for file in files:
        content = await file.read()
        text_content = ""
        filename_lower = file.filename.lower()

        if filename_lower.endswith('.pdf') and PYPDF_AVAILABLE:
            try:
                pdf_file = io.BytesIO(content)
                reader = PdfReader(pdf_file)
                text_content = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            except Exception:
                continue
        elif filename_lower.endswith('.docx') and OFFICE_AVAILABLE:
            try:
                docx_file = io.BytesIO(content)
                doc = Document(docx_file)
                text_content = "\n".join([p.text for p in doc.paragraphs if p.text])
            except Exception:
                continue
        elif filename_lower.endswith('.xlsx') and OFFICE_AVAILABLE:
            try:
                xlsx_file = io.BytesIO(content)
                wb = openpyxl.load_workbook(xlsx_file, data_only=True)
                excel_rows = []
                for sheet in wb.worksheets:
                    excel_rows.append(f"--- Sheet: {sheet.title} ---")
                    for row in sheet.iter_rows(values_only=True):
                        row_data = [str(cell).strip() for cell in row if cell is not None]
                        if row_data:
                            excel_rows.append(" | ".join(row_data))
                text_content = "\n".join(excel_rows)
            except Exception:
                continue
        else:
            text_content = content.decode('utf-8', errors='ignore')

        if not text_content.strip():
            continue

        chunk_size = 800
        overlap = 150
        chunks = []
        start = 0
        while start < len(text_content):
            end = start + chunk_size
            chunks.append(text_content[start:end])
            start += chunk_size - overlap

        for idx, chunk in enumerate(chunks):
            try:
                rag_collection.add(
                    documents=[chunk],
                    ids=[f"{file.filename}_{idx}_{uuid.uuid4().hex[:6]}"],
                    metadatas=[{"source": file.filename}],
                )
            except Exception as e:
                print(f"Vector DB Error: {str(e)}")

    return {"status": "success"}


# ---------------------------------------------------------
# 9. المحادثة مع الذكاء الاصطناعي (حقن ذاكرة الشركة المشتركة)
# ---------------------------------------------------------
class ChatMessage(BaseModel):
    message: str
    model: str
    hwid: str  # تمت إضافة التحقق من هوية الجهاز في المحادثات أيضاً


def retrieve_context(query: str, n_results: int = RAG_N_RESULTS) -> dict:
    empty_result = {"context_text": "", "has_context": False, "sources": []}

    if not CHROMA_AVAILABLE:
        return empty_result

    try:
        client = get_chroma_client()
        rag_collection = client.get_or_create_collection(name="company_knowledge", embedding_function=_ollama_embedding_function)
        results = rag_collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        print(f"[RAG] فشل الاسترجاع من قاعدة المعرفة: {str(e)}")
        return empty_result

    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    if not documents:
        return empty_result

    accepted_chunks = []
    sources = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        if dist is None or dist > RAG_MAX_DISTANCE:
            continue
        source_name = (meta or {}).get("source", "مصدر غير معروف")
        accepted_chunks.append(
            f"[المصدر: {source_name} | درجة الصلة: {1 - dist:.2f}]\n{doc}"
        )
        sources.append(source_name)

    if not accepted_chunks:
        return empty_result

    return {
        "context_text": "\n\n---\n\n".join(accepted_chunks),
        "has_context": True,
        "sources": sorted(set(sources)),
    }


@app.post("/api/chat")
async def chat_endpoint(data: ChatMessage):
    # التحقق من أن الطلب وارد فعلاً من الجهاز المرخص بناءً على HWID
    if data.hwid != DEVICE_HWID:
        raise HTTPException(status_code=403, detail="Invalid Hardware ID")

    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    actual_name = OLLAMA_MODELS.get(data.model, "qwen2.5:1.5b")

    retrieval = retrieve_context(data.message)

    if not retrieval["has_context"]:
        general_system_prompt = """أنت 'مستشار Nexus'، المساعد الذكي المحلي للشركة (يعمل بالكامل دون اتصال بالإنترنت).
لا تتوفر لديك حالياً أي مقاطع من أرشيف الشركة ذات صلة بهذا السؤال، لذا أجب بشكل طبيعي ومباشر باستخدام معرفتك العامة وقدراتك كمساعد محادثة.
لا تذكر أرشيف الشركة أو المصادر أو أي عبارة تفيد بعدم توفر معلومات، إلا إذا كان السؤال يتطلب صراحةً بيانات داخلية للشركة لا تملكها فعلاً.
كن ودوداً ومختصراً ومفيداً."""

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post('http://localhost:11434/api/generate', json={
                    "model": actual_name,
                    "system": general_system_prompt,
                    "prompt": data.message,
                    "stream": False,
                })
                if response.status_code == 200:
                    result = response.json()
                    return {
                        "reply": result.get("response", ""),
                        "sources": [],
                        "grounded": False,
                    }
                else:
                    return {"reply": "Error communicating with local AI engine.", "sources": [], "grounded": False}
        except Exception as e:
            return {"reply": f"Local AI engine connection failed: {str(e)}", "sources": [], "grounded": False}

    system_prompt = """أنت 'مستشار Nexus'، الذكاء الاصطناعي السيادي السري للشركة.
تعليماتك الصارمة:
1. اعتمد بشكل أساسي وحصري على [بيانات الشركة الداخلية] المرفقة أدناه للإجابة.
2. كل مقطع مرفق معه اسم المصدر ودرجة الصلة — يجب أن تذكر اسم المصدر (أو المصادر) الذي اعتمدت عليه في نهاية إجابتك.
3. إذا كانت المقاطع المرفقة لا تحتوي فعلياً على إجابة للسؤال رغم إرفاقها، لا تخترع إجابة، بل قل بوضوح: 'هذه المعلومات غير متوفرة في الأرشيف المرفق للشركة.' ولا تذكر مصادر في هذه الحالة.
4. كن دقيقاً واحترافياً، ولا تضف معلومات من معرفتك العامة خارج المرفق.

[بيانات الشركة الداخلية المستخرجة]:
{context}
"""
    formatted_system_prompt = system_prompt.replace("{context}", retrieval["context_text"])

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post('http://localhost:11434/api/generate', json={
                "model": actual_name,
                "system": formatted_system_prompt,
                "prompt": f"سؤال المستخدم: {data.message}",
                "stream": False,
            })
            if response.status_code == 200:
                result = response.json()
                return {
                    "reply": result.get("response", ""),
                    "sources": retrieval["sources"],
                    "grounded": True,
                }
            else:
                return {"reply": "Error communicating with local AI engine.", "sources": [], "grounded": False}
    except Exception as e:
        return {"reply": f"Local AI engine connection failed: {str(e)}", "sources": [], "grounded": False}


# ---------------------------------------------------------
# 10. تشغيل السيرفر والواجهة المكتبيّة
# ---------------------------------------------------------
def find_free_port(preferred=8000):
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("لم يتم العثور على بورت متاح بين 8000 و 8019")


SERVER_PORT = find_free_port(8000)


def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="error")


def resume_network_sync_if_licensed():
    state = load_local_state()
    if state.get("active") and state.get("hwid") == DEVICE_HWID:
        start_network_sync()


if __name__ == "__main__":
    t = threading.Thread(target=start_fastapi)
    t.daemon = True
    t.start()

    time.sleep(2)
    resume_network_sync_if_licensed()

    try:
        window = webview.create_window(
            title="Sovereign AI - Enterprise Node",
            url=f"http://127.0.0.1:{SERVER_PORT}",
            width=1280,
            height=800,
            resizable=True,
        )
        webview.start()
    except Exception as e:
        print(f"Webview failed to start: {str(e)}")
