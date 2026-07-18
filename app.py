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
LICENSE_SERVER_URL = os.environ.get("LICENSE_SERVER_URL", "https://your-app.up.railway.app")

# ملف تخزين محلي بسيط لحالة الترخيص/المؤسسة على هذا الجهاز تحديداً
LOCAL_STATE_PATH = os.path.join(os.path.abspath("."), "device_state.json")

# منفذ chromadb المشترك على الشبكة (مختلف عن منفذ واجهة FastAPI الرئيسي)
SHARED_DB_PORT = 8001

RAG_DB_DIR = "./local_rag_db"  # يُستخدم فقط عندما يكون هذا الجهاز هو الـ Host

# مهلة السماح بالعمل أوفلاين إذا تعذّر الوصول لسيرفر الترخيص (بالأيام)
OFFLINE_GRACE_PERIOD_DAYS = 5

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


# ---------------------------------------------------------
# 6. إدارة الروابط الخارجية
# ---------------------------------------------------------
class BrowserRequest(BaseModel):
    url: str


@app.post("/api/open-external")
async def open_external_link(req: BrowserRequest):
    webbrowser.open(req.url)
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
    webbrowser.open(checkout_url)
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
async def upload_endpoint(files: List[UploadFile] = File(...)):
    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    if not CHROMA_AVAILABLE:
        raise HTTPException(status_code=500, detail="Vector Database not available")

    client = get_chroma_client()
    rag_collection = client.get_or_create_collection(name="company_knowledge")

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


def retrieve_context(query: str, n_results: int = RAG_N_RESULTS) -> dict:
    """
    يسترجع أقرب المقاطع من قاعدة المعرفة، ويطبّق فحصاً برمجياً على درجة
    التشابه (distance) قبل اعتبار أي مقطع "سياقاً صالحاً".

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
        rag_collection = client.get_or_create_collection(name="company_knowledge")
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
    state = load_local_state()
    if not state.get("active"):
        raise HTTPException(status_code=403, detail="Unlicensed")

    actual_name = OLLAMA_MODELS.get(data.model, "qwen2.5:1.5b")

    # سحب بيانات الشركة من قاعدة المعرفة المشتركة (محلية إن كنا Host، أو عبر الشبكة إن كنا Client)
    # مع فحص برمجي لمدى الصلة قبل إرسال أي شيء للنموذج أصلاً.
    retrieval = retrieve_context(data.message)

    if not retrieval["has_context"]:
        # لا نرسل السؤال للنموذج أصلاً بدون سياق كافٍ — نمنع التخمين من جذوره
        # بدل الاعتماد فقط على تعليمة نصية قد يتجاهلها النموذج.
        return {
            "reply": "هذه المعلومات غير متوفرة في الأرشيف المرفق للشركة.",
            "sources": [],
            "grounded": False,
        }

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
