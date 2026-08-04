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
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel
import webview

from network_sync import OrgNetworkManager, NetworkState
import auth_rbac
from web_search_local import search_web, format_search_context

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

# عنوان سيرفر الترخيص المركزي (Railway) — لا يحتوي على أي أسرار حساسة هنا
LICENSE_SERVER_URL = os.environ.get("LICENSE_SERVER_URL", "https://web-production-de335.up.railway.app")

# ملف تخزين محلي بسيط لحالة الترخيص/المؤسسة على هذا الجهاز تحديداً
LOCAL_STATE_PATH = os.path.join(os.path.abspath("."), "device_state.json")

# منفذ chromadb المشترك على الشبكة (مختلف عن منفذ واجهة FastAPI الرئيسي)
SHARED_DB_PORT = 8001

RAG_DB_DIR = "./local_rag_db"  # يُستخدم فقط عندما يكون هذا الجهاز هو الـ Host

# قاعدة بيانات الموظفين والصلاحيات (RBAC) — محلية بالكامل، بنفس مجلد RAG.
# تُنشأ على أي جهاز يشغّل التطبيق، لكنها لا تُستخدم فعلياً إلا على جهاز الـ Host
# (أجهزة الـ Client توجّه طلبات المصادقة له عبر الشبكة المحلية — انظر أسفل).
os.makedirs(RAG_DB_DIR, exist_ok=True)
auth_rbac.init_auth_db(RAG_DB_DIR)

# مهلة السماح بالعمل أوفلاين إذا تعذّر الوصول لسيرفر الترخيص (بالأيام)
OFFLINE_GRACE_PERIOD_DAYS = 5

# ---------------------------------------------------------
# نظام طلب التقييم داخل التطبيق (In-App Feedback)
# ---------------------------------------------------------
FEEDBACK_STATE_PATH = os.path.join(os.path.abspath("."), "feedback_state.json")
FEEDBACK_PROMPT_AFTER_DAYS = int(os.environ.get("FEEDBACK_PROMPT_AFTER_DAYS", "3"))


def load_feedback_state() -> dict:
    if not os.path.exists(FEEDBACK_STATE_PATH):
        return {}
    try:
        with open(FEEDBACK_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_feedback_state(state: dict):
    with open(FEEDBACK_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# إعدادات جودة الاسترجاع (RAG Retrieval Quality)
# ---------------------------------------------------------
# chromadb مع sentence-transformers يستخدم افتراضياً L2 distance (مسافة إقليدية)
# وليس cosine similarity 0-1. القيمة الأصغر = تشابه أكبر (عكس الـ similarity score).
# هذي القيمة ابتدائية معقولة، لكن يجب ضبطها فعلياً على بياناتك (شرح الضبط تحت).
RAG_MAX_DISTANCE = float(os.environ.get("RAG_MAX_DISTANCE", "0.8"))

# عدد أقصى من المقاطع (chunks) التي نرسلها كسياق لكل سؤال
RAG_N_RESULTS = int(os.environ.get("RAG_N_RESULTS", "4"))


# ---------------------------------------------------------
# 1. هوية الجهاز (HWID) — بصمة ثابتة لكل جهاز
# ---------------------------------------------------------
def get_hardware_id() -> str:
    """
    يولّد بصمة شبه ثابتة للجهاز بدون الاعتماد على مكتبات خارجية إضافية،
    عبر دمج معرّفات نظام التشغيل الأساسية ثم عمل hash عليها.
    ⚠️ هذه بصمة "معقولة" لا "قطعية" — تغييرات جذرية بالعتاد (تغيير القرص
    الرئيسي مثلاً) قد تغيّرها. هذا مقبول لغرض عد الأجهزة، وليس تشفيراً أمنياً.
    """
    try:
        system_info = f"{platform.node()}-{platform.system()}-{platform.machine()}-{platform.processor()}"
    except Exception:
        system_info = str(uuid.getnode())  # احتياطي: عنوان MAC
    return hashlib.sha256(system_info.encode()).hexdigest()[:32]


DEVICE_HWID = get_hardware_id()


# ---------------------------------------------------------
# 2. تخزين حالة الترخيص محلياً (Cache) للسماح بالعمل المؤقت أوفلاين
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
    """
    يتحقق من صلاحية الترخيص عبر سيرفر Railway المركزي، بناءً على hwid هذا
    الجهاز فقط (لا يوجد مفهوم "مفتاح مؤسسة" — كل جهاز مشترك مستقل).
    عند النجاح: يحدّث الكاش المحلي بحالة جديدة + وقت التحقق.
    عند الفشل (لا إنترنت مثلاً): يرجع لحالة الكاش المحلي ضمن فترة السماح.
    """
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
            # السيرفر رد برفض صريح — لا نمنح فترة سماح هنا لأن هذا رفض واضح لا انقطاع اتصال
            return {"valid": False, "plan": None, "source": "server_rejected"}

    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
        # تعذّر الوصول للسيرفر (على الأغلب لا يوجد إنترنت) — نستخدم الكاش المحلي
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
    """
    يُستدعى بعد إتمام المستخدم للدفع عبر PayPal مباشرة من هذا الجهاز.
    السيرفر (Railway) هو من يتحقق فعلياً من صحة الطلب مع PayPal باستخدام
    السر المحفوظ هناك فقط، ثم يفعّل اشتراك هذا الـ hwid تحديداً.
    لا يوجد أي "مفتاح" ينتقل بين المستخدم والتطبيق في هذه العملية.
    """
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
    """
    يطلب من Railway منح هذا الجهاز تجربة مجانية (7 أيام بصلاحيات Ultra كاملة،
    بدون أي دفع أو بطاقة). القرار النهائي (هل هذا الجهاز استخدم تجربته من
    قبل أم لا) يُحسم بالكامل على الخادم البعيد اعتماداً على hwid، وليس على
    أي ملف محلي في هذا الجهاز — لذا حذف device_state.json محلياً لا يمنح
    تجربة ثانية.
    """
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
    """
    يشغّل سيرفر chromadb HTTP فعلياً على SHARED_DB_PORT، ليصبح هذا الجهاز
    قابلاً للوصول من باقي الأجهزة على الشبكة كقاعدة بيانات مشتركة.
    يُستدعى مرة واحدة فقط عندما يصبح هذا الجهاز Host.
    """
    global _chroma_server_started
    with _chroma_server_lock:
        if _chroma_server_started:
            return
        _chroma_server_started = True

    try:
        # chromadb 0.5.x يوفر سيرفر FastAPI داخلي جاهز للتشغيل ببرمجية
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

    # إذا أصبح هذا الجهاز Host الآن (لأول مرة أو بعد تحول من client)، شغّل سيرفر chromadb
    if state.role == "host" and not was_host_before:
        threading.Thread(target=_start_chroma_http_server, daemon=True).start()


def start_network_sync():
    """
    يُفعَّل بعد التحقق من ترخيص صالح لهذا الجهاز. لا يحتاج أي مفتاح —
    القرار بشأن من يُسمح له بالانضمام كعميل يُتَّخذ لاحقاً على Railway
    بناءً على خطة هذا الجهاز (انظر network_sync.py و server.py).
    """
    global network_manager
    if network_manager:
        return  # مُفعّل مسبقاً
    device_name = platform.node() or "جهاز غير مسمّى"
    network_manager = OrgNetworkManager(
        my_hwid=DEVICE_HWID,
        license_server_url=LICENSE_SERVER_URL,
        local_port=SHARED_DB_PORT,
        device_name=device_name,
    )
    # يعمل بخيط منفصل لأن الاكتشاف الأولي قد يستغرق ثوانٍ (DISCOVERY_TIMEOUT_SECONDS)
    threading.Thread(
        target=network_manager.start,
        kwargs={"on_state_change": on_network_state_change},
        daemon=True,
    ).start()



def get_chroma_client():
    """
    يرجع عميل chromadb مناسب حسب دور هذا الجهاز الحالي:
    - Host أو Standalone: قاعدة بيانات محلية فعلية (وهي نفسها التي تُقدَّم للشبكة).
    - Client: اتصال HTTP بقاعدة بيانات الجهاز المضيف عبر الشبكة المحلية.
    """
    with network_state_lock:
        role = current_network_state["role"]
        host_ip = current_network_state["host_ip"]
        host_port = current_network_state.get("host_port", SHARED_DB_PORT)

    if role == "client" and host_ip:
        # جهاز عادي متصل بمضيف على الشبكة
        return chromadb.HttpClient(host=host_ip, port=host_port)
    elif role == "host":
        # هذا الجهاز هو المضيف نفسه: يجب أن يتحدث مع سيرفر chromadb الذي
        # يشغّله بنفسه عبر HTTP (localhost) بدلاً من فتح نفس ملف القاعدة
        # مباشرة، لتفادي تعارض قفل الملف بين عمليتين (السيرفر + هذا الطلب).
        return chromadb.HttpClient(host="127.0.0.1", port=host_port)
    else:
        # Standalone: لا يوجد ترخيص/شبكة مفعّلة بعد — قاعدة محلية بحتة مؤقتة
        return chromadb.PersistentClient(path=RAG_DB_DIR)


# اسم نموذج التضمين الحالي — لو غيّرته يوماً (مثلاً لنموذج embedding أحدث
# أو أكبر)، فقط غيّر هذه القيمة. النظام أدناه سيكتشف تلقائياً أن المستندات
# القديمة تستخدم نموذجاً مختلفاً، ويمكن إعادة فهرستها عبر /api/admin/reindex
# دون أن يحتاج المستخدم لإعادة رفع أي ملف يدوياً.
OLLAMA_EMBEDDING_MODEL = os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")


class OllamaEmbeddingFunction:
    """
    دالة تضمين مخصّصة لـ chromadb تستخدم Ollama المحلي (عبر /api/embeddings)
    بدل دالة chromadb الافتراضية (ONNXMiniLM_L6_V2)، التي تحاول تحميل
    نموذجها من الإنترنت (chroma-onnx-models.s3.amazonaws.com) عند أول استخدام.
    هذا يكسر مبدأ العمل بدون اتصال بالإنترنت الذي يقوم عليه التطبيق بالكامل.
    يتطلب أن يكون نموذج OLLAMA_EMBEDDING_MODEL مسحوباً مسبقاً عبر:
        ollama pull nomic-embed-text
    """

    def __init__(self, model_name: str = OLLAMA_EMBEDDING_MODEL, base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.base_url = base_url

    def name(self) -> str:
        return f"ollama-{self.model_name}"

    def __call__(self, input):
        # واجهة chromadb الحديثة تستدعي الدالة باسم بارامتر "input" (قائمة نصوص)
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
# 5. نظام التتبع الحقيقي لتحميل نماذج الذكاء الاصطناعي (بدون تغيير)
# ---------------------------------------------------------
download_status = {
    "flash": {"status": "idle", "progress": 0},
    "plus": {"status": "idle", "progress": 0},
    "pro": {"status": "idle", "progress": 0},
}

OLLAMA_MODELS = {
    "flash": "qwen3.5:2b",
    "plus": "qwen3.5:9b",
    "pro": "gemma4:31b",
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
    actual_name = OLLAMA_MODELS.get(model_key, "qwen3.5:2b")
    bg_tasks.add_task(real_pull_model_task, model_key, actual_name)
    return {"status": "started", "actual_model": actual_name}


@app.get("/api/models/progress")
async def get_model_progress(model: str):
    return download_status.get(model, {"status": "idle", "progress": 0})


@app.get("/api/models/installed")
async def get_installed_models():
    """
    يستعلم فعلياً عن Ollama (عبر /api/tags) لمعرفة أي من نماذجنا
    (flash/plus/pro) مثبت فعلياً على هذا الجهاز، بدل افتراض أي شيء
    مسبقاً بالواجهة الأمامية.
    """
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
        # Ollama قد يرجع الاسم مع أو بدون لاحقة ":latest" حسب الإصدار
        if actual_name in installed_actual_names or f"{actual_name}:latest" in installed_actual_names
    ]
    return {"installed": installed_keys}


# ---------------------------------------------------------
# طلب التقييم (Feedback / Reviews)
# ---------------------------------------------------------
@app.get("/api/feedback/should-show")
async def feedback_should_show():
    """
    تستدعيها الواجهة عند فتح التطبيق لتقرر هل تعرض نافذة طلب التقييم.
    المنطق: أول مرة يفتح فيها المستخدم التطبيق نسجّل first_seen_at.
    بعد مرور FEEDBACK_PROMPT_AFTER_DAYS ولم يُقدَّم تقييم بعد، نطلب منه ذلك.
    يُعرض مرة واحدة فقط (حتى لو أغلقها المستخدم دون تقييم، لا نزعجه مرة ثانية
    تلقائياً — لكن يمكنه لاحقاً إرسال رأيه من قائمة الإعدادات إن أردت إضافة ذلك).
    """
    state = load_feedback_state()

    if "first_seen_at" not in state:
        state["first_seen_at"] = time.time()
        save_feedback_state(state)
        return {"show": False}

    if state.get("submitted") or state.get("dismissed"):
        return {"show": False}

    days_elapsed = (time.time() - state["first_seen_at"]) / 86400
    return {"show": days_elapsed >= FEEDBACK_PROMPT_AFTER_DAYS}


class FeedbackDismissRequest(BaseModel):
    permanently: bool = True


@app.post("/api/feedback/dismiss")
async def feedback_dismiss(data: FeedbackDismissRequest):
    """يُستدعى لو المستخدم أغلق نافذة التقييم دون إرسال رأيه."""
    state = load_feedback_state()
    if data.permanently:
        state["dismissed"] = True
    save_feedback_state(state)
    return {"ok": True}


class FeedbackSubmitRequest(BaseModel):
    rating: int
    comment: str = ""
    consent_to_publish: bool = False
    display_name: str = ""
    organization: str = ""


@app.post("/api/feedback/submit")
async def feedback_submit(data: FeedbackSubmitRequest):
    """
    يُرسل تقييم المستخدم لسيرفر Railway المركزي (مرتبطاً بـ hwid هذا
    الجهاز، بدون أي معلومة تعريفية أخرى إلا لو وافق المستخدم صراحة على
    نشر اسمه/جهته عبر consent_to_publish).
    """
    if not (1 <= data.rating <= 5):
        raise HTTPException(status_code=400, detail="التقييم يجب أن يكون بين 1 و 5")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{LICENSE_SERVER_URL}/api/feedback/submit",
                json={
                    "hwid": DEVICE_HWID,
                    "rating": data.rating,
                    "comment": data.comment,
                    "consent_to_publish": data.consent_to_publish,
                    "display_name": data.display_name,
                    "organization": data.organization,
                },
            )
        server_ok = resp.status_code == 200
    except Exception:
        server_ok = False

    # نسجّل محلياً أنه تم الإرسال بغض النظر عن نجاح الوصول للسيرفر،
    # حتى لا نزعج المستخدم بطلب التقييم مرة أخرى. لو فشل الإرسال فعلياً
    # (لا إنترنت)، نحتفظ بنسخة محلية يمكن مراجعتها لاحقاً يدوياً إن أردت.
    state = load_feedback_state()
    state["submitted"] = True
    if not server_ok:
        pending = state.get("pending_unsent", [])
        pending.append(data.dict())
        state["pending_unsent"] = pending
    save_feedback_state(state)

    return {"success": True, "synced_to_server": server_ok}


# ---------------------------------------------------------
# 6. إدارة الروابط الخارجية
# ---------------------------------------------------------
class BrowserRequest(BaseModel):
    url: str


def open_browser_reliably(url: str) -> bool:
    """
    يفتح رابطاً بالمتصفح الافتراضي، ويرجع حالة نجاح حقيقية (وليس افتراضاً
    أعمى). هذا ضروري لأن PyInstaller (--onefile) على لينكس تحديداً يضبط
    مؤقتاً متغير بيئة LD_LIBRARY_PATH ليشير لمكتبات مضمَّنة داخل الحزمة
    نفسها أثناء تشغيل التطبيق. هذا يعمل صحيحاً للتطبيق نفسه، لكنه يكسر
    غالباً أي عملية خارجية (subprocess) يُطلقها التطبيق — مثل المتصفح —
    لأن المتصفح الخارجي يحاول تحميل مكتباته الخاصة (GTK, glib, إلخ) لكنه
    يجد بدلاً منها نسخاً مضمَّنة داخل حزمة PyInstaller غير متوافقة معه،
    فيفشل الإطلاق بصمت دون رفع أي استثناء يلتقطه webbrowser.open().

    الحل: ننظّف نسخة من متغيرات البيئة (خاصة LD_LIBRARY_PATH) قبل تشغيل
    المتصفح كعملية منفصلة تماماً، حتى يستخدم مكتبات النظام الحقيقية بدل
    مكتبات PyInstaller المضمَّنة.
    """
    # بيئة نظيفة للعملية الفرعية: نحذف LD_LIBRARY_PATH الذي يضبطه PyInstaller
    # مؤقتاً (يظهر عادة كـ _MEIPASS أو مسار مؤقت يحتوي على مكتبات الحزمة)
    clean_env = os.environ.copy()
    clean_env.pop("LD_LIBRARY_PATH", None)
    # PyInstaller أحياناً يحفظ القيمة الأصلية هنا قبل الكتابة فوقها؛ نستعيدها إن وُجدت
    original_ld_path = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if original_ld_path:
        clean_env["LD_LIBRARY_PATH"] = original_ld_path

    # محاولة 1: أوامر النظام المباشرة (الأكثر موثوقية على لينكس تحديداً)
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
            os.startfile(url)  # noqa: available on Windows only
            return True
        except Exception:
            pass

    # محاولة 2 (احتياط أخير): webbrowser القياسية، بنفس البيئة النظيفة
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


@app.post("/api/open-external")
async def open_external_link(req: BrowserRequest):
    success = open_browser_reliably(req.url)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="تعذّر فتح المتصفح تلقائياً. افتح هذا الرابط يدوياً: " + req.url,
        )
    return {"status": "opened"}


# عنوان صفحة الدفع الخارجية. بما أنها تُخدَّم من نفس خادم Railway (عبر
# endpoint /checkout في server.py)، تُبنى تلقائياً من LICENSE_SERVER_URL —
# لا حاجة لضبط متغير بيئة منفصل إلا إذا نقلت صفحة الدفع لاستضافة أخرى مستقبلاً.
CHECKOUT_PAGE_URL = os.environ.get("CHECKOUT_PAGE_URL", f"{LICENSE_SERVER_URL}/checkout")


@app.post("/api/subscribe/open-checkout")
async def open_checkout(plan: str):
    """
    يفتح صفحة الدفع الخارجية في متصفح المستخدم، مع تمرير hwid هذا الجهاز
    والخطة المطلوبة كـ query params حتى تعرف صفحة الدفع لأي جهاز تُصدر
    order_id بعد نجاح الدفع عبر PayPal (تُستخدم لاحقاً في /api/subscribe/verify).
    """
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
# 7. الاشتراك المباشر عبر PayPal (بدون أي مفتاح نصي)
# ---------------------------------------------------------
class VerifyOrderRequest(BaseModel):
    order_id: str
    plan: str  # مثال: "pro_monthly", "pro_yearly", "ultra_monthly", "ultra_yearly"


@app.post("/api/subscribe/verify")
async def subscribe_verify_api(data: VerifyOrderRequest):
    """
    يُستدعى بعد أن يتم المستخدم عملية الدفع عبر PayPal داخل المتصفح
    (نافذة خارجية تُفتح عبر /api/subscribe/open-checkout). التحقق الفعلي
    من صحة الدفع يحدث على Railway، وهو من يُفعّل الاشتراك المرتبط بـ HWID
    هذا الجهاز تحديداً — لا يُدخل المستخدم أي مفتاح نصي في أي خطوة.
    """
    device_name = platform.node() or "جهاز غير مسمّى"
    result = await verify_paypal_order_with_server(data.order_id, DEVICE_HWID, data.plan, device_name)

    if result.get("success"):
        start_network_sync()

    return result


@app.post("/api/trial/start")
async def trial_start_api():
    """
    يُستدعى من الواجهة عند أول تشغيل للتطبيق (قبل أي دفع)، لبدء تجربة
    مجانية 7 أيام بصلاحيات Ultra كاملة. Railway هو من يقرر إن كان هذا
    الجهاز (hwid) قد استخدم تجربته من قبل أم لا؛ هذا الـ endpoint لا
    يستطيع منح تجربة ثانية بأي حال حتى لو استُدعي عدة مرات.
    """
    device_name = platform.node() or "جهاز غير مسمّى"
    result = await start_free_trial_with_server(DEVICE_HWID, device_name)

    if result.get("success"):
        start_network_sync()

    return result


@app.get("/api/check-license")
async def check_license_api():
    """يُستدعى دورياً من الواجهة الأمامية للتحقق من حالة الترخيص الحالية."""
    result = await verify_license_with_server(DEVICE_HWID)

    # إذا كان الترخيص صالحاً ولم تُفعَّل مزامنة الشبكة بعد (مثلاً بعد إعادة تشغيل التطبيق)
    if result.get("valid") and network_manager is None:
        start_network_sync()

    return result



@app.get("/health")
def health_check():
    """
    يُستخدم من قبل أجهزة أخرى على الشبكة للتحقق من توفر هذا الجهاز
    (كمضيف محتمل) ومعرفة هويته (hwid) قبل محاولة الانضمام إليه رسمياً
    عبر Railway. لا يكشف أي معلومة حساسة، فقط hwid ودور هذا الجهاز الحالي.
    """
    with network_state_lock:
        role = current_network_state["role"]
    return {"status": "ok", "hwid": DEVICE_HWID, "role": role}


@app.get("/api/network-status")
def network_status_api():
    """تعرض حالة الاتصال الحالية بالشبكة (Host/Client/Standalone) للواجهة."""
    with network_state_lock:
        return dict(current_network_state)


class ConnectManualRequest(BaseModel):
    host_ip: str
    host_port: int = SHARED_DB_PORT


# ---------------------------------------------------------
# نظام تسجيل الدخول والصلاحيات (RBAC) — محلي بالكامل
# ---------------------------------------------------------
#
# مبدأ التوجيه (Routing):
# - جهاز الـ Host: يعالج طلبات المصادقة مباشرة من قاعدته المحلية (SQLite).
# - جهاز الـ Client: لا يملك نسخة من بيانات الموظفين (بالتصميم — لا يوجد
#   أي تكرار أو تسريب لبيانات الحسابات). فيوجّه (يمرّر) نفس الطلب لجهاز
#   الـ Host عبر الشبكة المحلية فقط (LAN)، تماماً بنفس فلسفة مشاركة
#   قاعدة RAG المستخدمة أصلاً بالتطبيق. لا يخرج أي طلب خارج شبكة المكتب.

def _get_host_base_url() -> Optional[str]:
    """يرجع رابط جهاز الـ Host على الشبكة المحلية، أو None لو نحن الـ Host فعلياً أو غير متصلين."""
    with network_state_lock:
        role = current_network_state["role"]
        host_ip = current_network_state.get("host_ip")
        host_port = current_network_state.get("host_port", SHARED_DB_PORT)
    if role != "client" or not host_ip:
        return None
    # منفذ FastAPI الرئيسي للـ Host، وليس منفذ chromadb المشترك
    return f"http://{host_ip}:{SERVER_PORT}"


class RegisterRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register")
async def auth_register(data: RegisterRequest):
    """
    تسجيل حساب موظف جديد (Self-service). الحساب يُنشأ بحالة 'pending'
    تلقائياً — بدون أي صلاحيات — حتى يوافق عليه الأدمن (جهاز الـ Host).
    """
    with network_state_lock:
        role = current_network_state["role"]

    if role == "client":
        host_url = _get_host_base_url()
        if not host_url:
            raise HTTPException(status_code=503, detail="تعذّر الوصول لجهاز الأدمن على الشبكة المحلية")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{host_url}/api/auth/register", json=data.dict())
                return resp.json()
        except Exception:
            raise HTTPException(status_code=503, detail="تعذّر الاتصال بجهاز الأدمن")

    result = auth_rbac.register_employee(data.username, data.password)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)
    return {"success": True, "message": "تم إنشاء الحساب، بانتظار موافقة الأدمن"}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def auth_login(data: LoginRequest):
    with network_state_lock:
        role = current_network_state["role"]

    if role == "client":
        host_url = _get_host_base_url()
        if not host_url:
            raise HTTPException(status_code=503, detail="تعذّر الوصول لجهاز الأدمن على الشبكة المحلية")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{host_url}/api/auth/login", json=data.dict())
                return resp.json()
        except Exception:
            raise HTTPException(status_code=503, detail="تعذّر الاتصال بجهاز الأدمن")

    result = auth_rbac.login_employee(data.username, data.password)
    if not result.success:
        raise HTTPException(status_code=401, detail=result.error)

    return {
        "success": True,
        "status": result.status,  # 'pending' أو 'approved'
        "token": result.token,
        "assigned_tags": result.assigned_tags,
    }


async def get_current_employee(request: Request) -> Optional[dict]:
    """
    يستخرج بيانات الموظف من هيدر Authorization (Bearer token)، ويعمل
    سواء كنا Host (تحقق محلي) أو Client (تمرير التحقق للـ Host عبر الشبكة).
    يرجع None لو ما فيه توكن أو كان غير صالح (يعامَل كزائر بدون صلاحيات خاصة).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    if not token:
        return None

    with network_state_lock:
        role = current_network_state["role"]

    if role == "client":
        host_url = _get_host_base_url()
        if not host_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{host_url}/api/auth/session",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return None

    return auth_rbac.get_session(token)


@app.get("/api/auth/session")
def auth_session(request: Request):
    """يُستخدم داخلياً (من جهاز Client لجهاز Host) للتحقق من صلاحية توكن جلسة."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="لا يوجد توكن")
    token = auth_header[len("Bearer "):].strip()
    session = auth_rbac.get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="جلسة غير صالحة")
    return session


def _require_admin(request: Request):
    """
    الأدمن = جهاز الـ Host نفسه فقط. نتحقق أن الطلب يصل من نفس الجهاز
    (loopback) وليس عبر الشبكة من جهاز Client — لوحة التحكم بالصلاحيات
    لا تُعرض ولا تُستدعى إلا محلياً على جهاز صاحب النظام.
    """
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="هذه الصلاحية متاحة فقط من جهاز الأدمن نفسه")


@app.get("/api/admin/employees")
def admin_list_employees(request: Request):
    _require_admin(request)
    return {"employees": auth_rbac.list_employees()}


class SetAccessRequest(BaseModel):
    employee_id: int
    status: str  # 'approved' | 'disabled' | 'pending'
    assigned_tags: List[str] = []


@app.post("/api/admin/employees/access")
def admin_set_employee_access(request: Request, data: SetAccessRequest):
    _require_admin(request)
    ok = auth_rbac.set_employee_access(data.employee_id, data.status, data.assigned_tags)
    if not ok:
        raise HTTPException(status_code=404, detail="لم يتم العثور على الموظف")
    return {"success": True}


@app.post("/api/admin/employees/{employee_id}/delete")
def admin_delete_employee(request: Request, employee_id: int):
    _require_admin(request)
    ok = auth_rbac.delete_employee(employee_id)
    if not ok:
        raise HTTPException(status_code=404, detail="لم يتم العثور على الموظف")
    return {"success": True}


@app.post("/api/network-connect-manual")
def network_connect_manual(data: ConnectManualRequest):
    """
    خيار احتياطي لإدخال IP الجهاز المضيف يدوياً، لحالات حجب mDNS
    ببعض إعدادات الشبكات المؤسسية. يمر بنفس تحقق Railway الإلزامي
    مثل الاكتشاف التلقائي تماماً — لا اختصار هنا أيضاً.
    """
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
async def upload_endpoint(request: Request, files: List[UploadFile] = File(...), tags: str = ""):
    """
    tags: قائمة وسوم مفصولة بفاصلة (مثال: "legal-team,confidential").
    لو تُركت فارغة، الملف يبقى مرئياً لكل من له وصول عام (سلوك سابق كما هو).
    يُستخدم لاحقاً لفلترة النتائج حسب صلاحيات الموظف عند الاسترجاع (RAG).
    """
    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    if not CHROMA_AVAILABLE:
        raise HTTPException(status_code=500, detail="Vector Database not available")

    file_tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags_str = ",".join(sorted(set(file_tags)))

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
                    metadatas=[{
                        "source": file.filename,
                        "tags": tags_str,
                        # نسجّل بأي نموذج embedding تم توليد هذا المتجه، حتى نعرف
                        # لاحقاً أي المستندات "قديمة" (بنموذج مختلف عن الحالي)
                        # وتحتاج إعادة فهرسة بعد أي تحديث لنموذج التضمين نفسه.
                        "embedding_model": OLLAMA_EMBEDDING_MODEL,
                    }],
                )
            except Exception as e:
                print(f"Vector DB Error: {str(e)}")

    return {"status": "success"}


# ---------------------------------------------------------
# إعادة فهرسة قاعدة المعرفة عند تحديث نموذج الـ Embedding
# ---------------------------------------------------------
reindex_status = {"status": "idle", "total": 0, "done": 0, "error": None}


async def _run_reindex_task():
    """
    يمر على كل المستندات المخزّنة في chromadb، ويعيد توليد embedding لأي
    مستند لم يُخزَّن بنموذج OLLAMA_EMBEDDING_MODEL الحالي (أي مستندات
    قديمة من نموذج تضمين سابق). النص الأصلي والمصدر والوسوم تبقى كما هي
    تماماً — فقط المتجه الرقمي (embedding) يُعاد توليده. هذا يشغّل بخيط
    خلفي لأنه قد يستغرق وقتاً حسب حجم قاعدة المعرفة.
    """
    global reindex_status
    reindex_status = {"status": "running", "total": 0, "done": 0, "error": None}

    try:
        client = get_chroma_client()
        rag_collection = client.get_or_create_collection(
            name="company_knowledge", embedding_function=_ollama_embedding_function
        )

        # نجيب كل شيء (بدون حد أقصى) مع النصوص والميتاداتا
        all_docs = rag_collection.get(include=["documents", "metadatas"])
        ids = all_docs.get("ids", [])
        documents = all_docs.get("documents", [])
        metadatas = all_docs.get("metadatas", [])

        # نحدد فقط المستندات القديمة (بنموذج تضمين مختلف عن الحالي، أو بلا تسجيل أصلاً)
        to_reindex = [
            (doc_id, doc, meta)
            for doc_id, doc, meta in zip(ids, documents, metadatas)
            if (meta or {}).get("embedding_model") != OLLAMA_EMBEDDING_MODEL
        ]

        reindex_status["total"] = len(to_reindex)

        for doc_id, doc, meta in to_reindex:
            new_meta = dict(meta or {})
            new_meta["embedding_model"] = OLLAMA_EMBEDDING_MODEL

            # rag_collection.update() تُعيد توليد الـ embedding تلقائياً عند
            # تمرير "documents" من جديد، لأن embedding_function المرتبطة
            # بالـ collection تُستدعى مجدداً على النص نفسه.
            rag_collection.update(
                ids=[doc_id],
                documents=[doc],
                metadatas=[new_meta],
            )
            reindex_status["done"] += 1

        reindex_status["status"] = "completed"

    except Exception as e:
        reindex_status["status"] = "error"
        reindex_status["error"] = str(e)


@app.post("/api/admin/reindex")
async def admin_reindex(request: Request, bg_tasks: BackgroundTasks):
    """
    يُشغَّل يدوياً من جهاز الأدمن (Host) بعد تحديث OLLAMA_EMBEDDING_MODEL
    لنموذج جديد. يبدأ إعادة الفهرسة بخيط خلفي، ويمكن متابعة تقدمها عبر
    /api/admin/reindex/status. لا يحتاج المستخدم إعادة رفع أي ملف —
    النصوص الأصلية محفوظة أصلاً في chromadb ويُعاد استخدامها مباشرة.
    """
    _require_admin(request)

    if reindex_status["status"] == "running":
        raise HTTPException(status_code=409, detail="عملية إعادة فهرسة أخرى قيد التنفيذ بالفعل")

    bg_tasks.add_task(_run_reindex_task)
    return {"status": "started"}


@app.get("/api/admin/reindex/status")
async def admin_reindex_status(request: Request):
    """يعرض تقدم عملية إعادة الفهرسة الحالية أو الأخيرة."""
    _require_admin(request)
    return reindex_status


@app.get("/api/admin/embedding-info")
async def admin_embedding_info(request: Request):
    """
    يعرض معلومات سريعة: النموذج الحالي المضبوط، وعدد المستندات القديمة
    (بنموذج مختلف) التي تحتاج إعادة فهرسة — مفيد لمعرفة هل /api/admin/reindex
    ضروري أصلاً قبل تشغيله.
    """
    _require_admin(request)

    try:
        client = get_chroma_client()
        rag_collection = client.get_or_create_collection(
            name="company_knowledge", embedding_function=_ollama_embedding_function
        )
        all_docs = rag_collection.get(include=["metadatas"])
        metadatas = all_docs.get("metadatas", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"تعذّر قراءة قاعدة المعرفة: {str(e)}")

    total = len(metadatas)
    outdated = sum(
        1 for meta in metadatas
        if (meta or {}).get("embedding_model") != OLLAMA_EMBEDDING_MODEL
    )

    return {
        "current_embedding_model": OLLAMA_EMBEDDING_MODEL,
        "total_documents": total,
        "outdated_documents": outdated,
        "needs_reindex": outdated > 0,
    }


# ---------------------------------------------------------
# 9. المحادثة مع الذكاء الاصطناعي (حقن ذاكرة الشركة المشتركة)
# ---------------------------------------------------------
class ChatMessage(BaseModel):
    message: str
    model: str
    enable_web_search: bool = False  # خيار المستخدم — معطّل افتراضياً (opt-in)


def retrieve_context(query: str, n_results: int = RAG_N_RESULTS, allowed_tags: Optional[List[str]] = None) -> dict:
    """
    يسترجع أقرب المقاطع من قاعدة المعرفة، ويطبّق فحصاً برمجياً على درجة
    التشابه (distance) قبل اعتبار أي مقطع "سياقاً صالحاً".

    allowed_tags: لو مُمرَّرة (موظف مسجّل دخول له صلاحيات محددة)، يُستبعد
    أي مقطع له وسم (tag) ولا يظهر ضمن allowed_tags. مقاطع بلا أي وسم
    (تُرفع بدون تحديد tags) تبقى مرئية للجميع دائماً — الوسم اختياري،
    فقط الملفات المصنَّفة صراحةً تُفلتَر.

    يرجع dict فيها:
      - context_text: النص الجاهز للحقن بالـ prompt (فارغ لو ما فيه نتائج كافية الصلة)
      - has_context: هل فيه سياق صالح فعلاً (bool)
      - sources: قائمة أسماء الملفات المستخدَمة فعلياً (للعرض/التتبع)
    """
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

    # الفحص البرمجي: نستبعد أي مقطع تجاوزت مسافته الحد الأقصى المسموح
    # (أي تشابه ضعيف رياضياً مع السؤال)، بدل ما نثق فقط بتعليمة النموذج النصية.
    accepted_chunks = []
    sources = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        if dist is None or dist > RAG_MAX_DISTANCE:
            continue

        # فلترة RBAC: لو المقطع له وسوم محددة، الموظف يحتاج واحد منها على الأقل
        chunk_tags_str = (meta or {}).get("tags", "")
        chunk_tags = [t for t in chunk_tags_str.split(",") if t]
        if chunk_tags and allowed_tags is not None:
            if not set(chunk_tags).intersection(set(allowed_tags)):
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
async def chat_endpoint(request: Request, data: ChatMessage):
    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    actual_name = OLLAMA_MODELS.get(data.model, "qwen3.5:2b")

    # لو موظف مسجّل دخول (له توكن صالح)، نجيب وسومه المسموحة لفلترة النتائج.
    # زائر بدون تسجيل دخول (أو جهاز standalone بدون نظام موظفين مفعّل)
    # يبقى يشوف كل المحتوى غير المصنَّف بوسوم — سلوك سابق كما هو.
    employee = await get_current_employee(request)
    allowed_tags = employee["assigned_tags"] if employee else None

    # سحب بيانات الشركة من قاعدة المعرفة المشتركة (محلية إن كنا Host، أو عبر الشبكة إن كنا Client)
    # مع فحص برمجي لمدى الصلة قبل إرسال أي شيء للنموذج أصلاً.
    retrieval = retrieve_context(data.message, allowed_tags=allowed_tags)

    # البحث بالإنترنت — اختياري تماماً (opt-in)، يصير مباشرة من هذا الجهاز
    # إلى محرك البحث فقط، بدون أي وسيط من طرفنا. يُفعَّل فقط لو المستخدم
    # حدد enable_web_search=true صراحةً بهذا الطلب.
    web_context_text = ""
    web_sources: List[str] = []
    if data.enable_web_search:
        web_results = await search_web(data.message)
        if web_results:
            web_context_text = format_search_context(web_results)
            web_sources = [r["url"] for r in web_results]

    has_any_context = retrieval["has_context"] or bool(web_context_text)

    if not has_any_context:
        # لا يوجد سياق كافٍ لا من أرشيف الشركة ولا من الويب (إن كان مفعّلاً).
        # هذا لا يعني أن السؤال خاطئ — قد يكون سؤالاً عاماً أو تعريفياً بسيطاً.
        # نسمح للنموذج بالرد كمساعد عام طبيعي، دون أي ذكر للأرشيف أو المصادر.
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

    # نبني قسم السياق: أرشيف الشركة (إن وُجد) + نتائج الويب (إن كانت مفعّلة وتوفرت)
    context_sections = []
    if retrieval["has_context"]:
        context_sections.append("[بيانات الشركة الداخلية]:\n" + retrieval["context_text"])
    if web_context_text:
        context_sections.append("[نتائج بحث ويب حيّة — من مصادر خارجية]:\n" + web_context_text)
    combined_context = "\n\n===\n\n".join(context_sections)

    system_prompt = """أنت 'مستشار Nexus'، الذكاء الاصطناعي السيادي السري للشركة.
تعليماتك الصارمة:
1. اعتمد بشكل أساسي على المقاطع المرفقة أدناه للإجابة (سواء من أرشيف الشركة الداخلي أو من نتائج بحث الويب المرفقة إن وُجدت).
2. كل مقطع مرفق معه اسم المصدر — يجب أن تذكر اسم المصدر (أو المصادر) الذي اعتمدت عليه في نهاية إجابتك. فرّق بوضوح بين معلومة من أرشيف الشركة ومعلومة من الويب.
3. إذا كانت المقاطع المرفقة لا تحتوي فعلياً على إجابة للسؤال رغم إرفاقها، لا تخترع إجابة، بل قل بوضوح أن المعلومة غير متوفرة في المرفق.
4. كن دقيقاً واحترافياً، ولا تضف معلومات من معرفتك العامة خارج المرفق دون توضيح أنها من معرفتك العامة.

[المقاطع المرفقة]:
{context}
"""
    formatted_system_prompt = system_prompt.replace("{context}", combined_context)

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
                    "sources": retrieval["sources"] + web_sources,
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
    """يتحقق إن كان البورت المفضل مشغولاً، ويرجع أول بورت متاح بدءاً منه."""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("لم يتم العثور على بورت متاح بين 8000 و 8019")


SERVER_PORT = find_free_port(8000)


def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="error")


def resume_network_sync_if_licensed():
    """عند إقلاع التطبيق: إذا كان هناك ترخيص مفعّل مسبقاً (كاش محلي)، أعد تفعيل مزامنة الشبكة تلقائياً."""
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
