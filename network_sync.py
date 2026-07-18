"""
SovereignAI - Network Sync Module (v2 — بدون org_key)
========================================================
مسؤول عن ربط أجهزة نفس المكتب على شبكة LAN المحلية لمشاركة قاعدة بيانات
واحدة (chromadb + الملفات المرفوعة)، دون الحاجة لسيرفر مخصص.

=====================================================================
التغيير الجوهري عن النسخة السابقة (v1):
=====================================================================
لم يعد هناك "مفتاح مؤسسة" نصي يُدخله المستخدم. كل جهاز له اشتراك خاص
به مرتبط بـ hwid الخاص به على خادم الترخيص (Railway). الجهاز الذي
يملك اشتراكاً يدعم LAN (Pro/Ultra) هو من "يستضيف" (Host)، وأي جهاز
آخر بنفس المكتب يقدر "ينضم" كعميل — لكن هذا الانضمام يمر إلزامياً
عبر تحقق من Railway (/api/session/join) الذي يقرر القبول أو الرفض
بناءً على خطة صاحب الـ Host وعدد الأجهزة المتصلة به فعلياً حالياً.

هذا القرار (القبول/الرفض) لا يمكن لأي جهاز عميل التلاعب به محلياً،
لأن الجهاز المضيف نفسه لا يستطيع "تمرير" عميل جديد بدون موافقة صريحة
من الخادم البعيد على كل محاولة انضمام.

⚠️ حدود معروفة (صريحة، بدون تجميل):
- هذا يعمل فقط ضمن نفس الشبكة المحلية (LAN) — لا يدعم اتصال عبر الإنترنت
  بين فروع متباعدة جغرافياً. هذا خارج نطاق هذه النسخة.
- Zeroconf/mDNS قد يُحجب من بعض إعدادات الشبكات المؤسسية الصارمة.
  في هذه الحالة يُوفَّر خيار احتياطي: إدخال IP الجهاز المضيف يدوياً.
- يتطلب اتصال إنترنت لحظي عند كل محاولة انضمام جهاز جديد (وعند كل
  heartbeat دوري) لأن القرار يُتَّخذ على الخادم البعيد لا محلياً.
"""

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, ServiceStateChange

SERVICE_TYPE = "_sovereignai._tcp.local."
DISCOVERY_TIMEOUT_SECONDS = 5       # مدة البحث عن Host موجود قبل اعتبار هذا الجهاز هو الـ Host
HOST_HEALTH_CHECK_INTERVAL = 15     # كل كم ثانية نتأكد إن الـ Host لسه شغال (فحص محلي بسيط)
SESSION_HEARTBEAT_INTERVAL = 45     # كل كم ثانية نرسل heartbeat لـ Railway لإبقاء الجلسة حيّة
# يجب أن يكون أصغر من SESSION_STALE_SECONDS في server.py (90 ثانية) بهامش كافٍ


def get_local_ip() -> str:
    """يحصل على IP الجهاز الفعلي على الشبكة المحلية (وليس 127.0.0.1)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@dataclass
class NetworkState:
    """الحالة الحالية لهذا الجهاز ضمن شبكة المكتب المحلية."""
    host_hwid: str                     # hwid صاحب الاشتراك المستضيف للشبكة (قد يكون هذا الجهاز نفسه)
    role: str = "unknown"              # "host" أو "client" أو "standalone" أو "rejected"
    host_ip: Optional[str] = None
    host_port: int = 8001
    is_connected: bool = False
    session_token: Optional[str] = None   # فقط عند role == "client"، من Railway
    rejection_reason: Optional[str] = None  # سبب الرفض إن وُجد (لعرضه بوضوح للمستخدم)
    on_state_change: Optional[Callable[["NetworkState"], None]] = field(default=None, repr=False)

    def _notify(self):
        if self.on_state_change:
            self.on_state_change(self)


class OrgNetworkManager:
    """
    يدير عملية اكتشاف/إعلان الجهاز على الشبكة، بالتنسيق مع خادم الترخيص
    البعيد (Railway) الذي يملك القرار النهائي بشأن قبول عملاء جدد.

    الاستخدام:
        manager = OrgNetworkManager(
            my_hwid="ABC123", license_server_url="https://your-app.up.railway.app",
            local_port=8001,
        )
        manager.start(on_state_change=my_callback)
    """

    def __init__(self, my_hwid: str, license_server_url: str, local_port: int = 8001,
                 device_name: str = ""):
        self.my_hwid = my_hwid
        self.license_server_url = license_server_url.rstrip("/")
        self.local_port = local_port
        self.device_name = device_name or socket.gethostname()

        self.zeroconf = Zeroconf()
        # عند البدء لا نعرف بعد هل سنكون host أم client؛ host_hwid يُحدَّث لاحقاً
        self.state = NetworkState(host_hwid=my_hwid, host_port=local_port)

        self._service_info: Optional[ServiceInfo] = None
        self._browser: Optional[ServiceBrowser] = None
        self._discovered_host: Optional[tuple] = None  # (ip, port, host_hwid)
        self._discovery_event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

    # -----------------------------------------------------------------
    # نقطة الدخول الرئيسية
    # -----------------------------------------------------------------

    def start(self, on_state_change: Optional[Callable[[NetworkState], None]] = None):
        self.state.on_state_change = on_state_change
        self._discover_existing_host()

        if self._discovered_host:
            ip, port, host_hwid = self._discovered_host
            self._try_become_client(ip, port, host_hwid)
        else:
            self._become_host()

        self._start_health_monitor()

    def stop(self):
        self._stop_flag.set()
        if self.state.role == "client" and self.state.session_token:
            self._leave_session_best_effort()
        if self._service_info:
            self.zeroconf.unregister_service(self._service_info)
        if self._browser:
            self._browser.cancel()
        self.zeroconf.close()

    # -----------------------------------------------------------------
    # اكتشاف Host موجود مسبقاً على الشبكة (أي host، بغض النظر عن هويته)
    # -----------------------------------------------------------------

    def _discover_existing_host(self):
        self._discovery_event.clear()
        self._discovered_host = None

        def on_service_state_change(zeroconf, service_type, name, state_change):
            if state_change != ServiceStateChange.Added:
                return
            info = zeroconf.get_service_info(service_type, name)
            if not info:
                return

            properties = {
                k.decode(): v.decode() for k, v in (info.properties or {}).items()
                if isinstance(k, bytes) and isinstance(v, bytes)
            }
            host_hwid = properties.get("host_hwid")
            if not host_hwid:
                return

            # لا نتصل بأنفسنا كـ client لو كنا نحن من أعلن الخدمة
            if host_hwid == self.my_hwid:
                return

            ip = socket.inet_ntoa(info.addresses[0])
            self._discovered_host = (ip, info.port, host_hwid)
            self._discovery_event.set()

        browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, handlers=[on_service_state_change])
        self._discovery_event.wait(timeout=DISCOVERY_TIMEOUT_SECONDS)
        browser.cancel()

    # -----------------------------------------------------------------
    # وضع Host: هذا الجهاز يصبح النقطة المركزية لقاعدة البيانات
    # -----------------------------------------------------------------
    # ملاحظة أمنية مهمة: أي جهاز يقدر "يعلن" نفسه Host محلياً عبر mDNS —
    # هذا لا يمنحه أي صلاحية فعلية، لأن أي عميل يحاول الانضمام إليه سيُرفض
    # من Railway ما لم يكن صاحب اشتراك Pro/Ultra ساري فعلاً بخادم الترخيص.

    def _become_host(self):
        local_ip = get_local_ip()

        self._service_info = ServiceInfo(
            SERVICE_TYPE,
            f"sovereignai-{self.my_hwid}._sovereignai._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=self.local_port,
            properties={"host_hwid": self.my_hwid},
        )
        self.zeroconf.register_service(self._service_info)

        self.state.role = "host"
        self.state.host_hwid = self.my_hwid
        self.state.host_ip = local_ip
        self.state.host_port = self.local_port
        self.state.is_connected = True
        self.state.rejection_reason = None
        self.state._notify()

    # -----------------------------------------------------------------
    # وضع Client: محاولة الانضمام لـ Host موجود — يتطلب موافقة Railway
    # -----------------------------------------------------------------

    def _try_become_client(self, host_ip: str, host_port: int, host_hwid: str):
        """
        يحاول الانضمام كعميل. القرار الفعلي (قبول/رفض) يأتي من Railway
        عبر /api/session/join — وليس من هذا الجهاز ولا من الجهاز المضيف.
        """
        try:
            resp = httpx.post(
                f"{self.license_server_url}/api/session/join",
                json={
                    "host_hwid": host_hwid,
                    "client_hwid": self.my_hwid,
                    "client_device_name": self.device_name,
                },
                timeout=10.0,
            )
        except httpx.HTTPError:
            # تعذّر الوصول لخادم الترخيص — لا يمكن التحقق، فلا نسمح بالانضمام.
            # نصبح Host مستقل مؤقتاً بدل تعليق الجهاز بلا حالة واضحة.
            self.state.role = "standalone"
            self.state.is_connected = False
            self.state.rejection_reason = "تعذّر الاتصال بخادم الترخيص للتحقق من إمكانية الانضمام"
            self.state._notify()
            self._become_host()
            return

        if resp.status_code == 200:
            data = resp.json()
            self.state.role = "client"
            self.state.host_hwid = host_hwid
            self.state.host_ip = host_ip
            self.state.host_port = host_port
            self.state.session_token = data.get("session_token")
            self.state.is_connected = True
            self.state.rejection_reason = None
            self.state._notify()
            self._start_heartbeat_loop()
        else:
            # مرفوض من Railway (خطة لا تدعم LAN، أو الحد الأقصى مكتمل، أو اشتراك المضيف منتهٍ)
            try:
                detail = resp.json().get("detail", "تم رفض الانضمام")
            except Exception:
                detail = "تم رفض الانضمام"
            self.state.role = "rejected"
            self.state.is_connected = False
            self.state.rejection_reason = detail
            self.state._notify()
            # لا نصبح Host تلقائياً هنا لتجنّب حلقة اكتشاف/رفض متكررة إن كان
            # هناك host واحد فقط بالشبكة وصل لحده الأقصى بالفعل.

    # -----------------------------------------------------------------
    # heartbeat دوري لإبقاء الجلسة حيّة على Railway (وضع Client فقط)
    # -----------------------------------------------------------------

    def _start_heartbeat_loop(self):
        def loop():
            while not self._stop_flag.is_set() and self.state.role == "client":
                time.sleep(SESSION_HEARTBEAT_INTERVAL)
                if self.state.role != "client" or not self.state.session_token:
                    continue
                try:
                    resp = httpx.post(
                        f"{self.license_server_url}/api/session/heartbeat",
                        json={"session_token": self.state.session_token},
                        timeout=8.0,
                    )
                    if resp.status_code == 404:
                        # الجلسة انتهت من طرف الخادم (مثلاً بسبب توقف طويل) — نحاول الانضمام من جديد
                        self._discover_existing_host()
                        if self._discovered_host:
                            ip, port, host_hwid = self._discovered_host
                            self._try_become_client(ip, port, host_hwid)
                        else:
                            self.state.role = "standalone"
                            self.state.is_connected = False
                            self.state._notify()
                except httpx.HTTPError:
                    pass  # فشل مؤقت بالشبكة — سيُعاد المحاولة بالدورة التالية

        self._heartbeat_thread = threading.Thread(target=loop, daemon=True)
        self._heartbeat_thread.start()

    def _leave_session_best_effort(self):
        try:
            httpx.post(
                f"{self.license_server_url}/api/session/leave",
                json={"session_token": self.state.session_token},
                timeout=5.0,
            )
        except httpx.HTTPError:
            pass

    # -----------------------------------------------------------------
    # مراقبة صحة الاتصال المحلي بالـ Host (وضع Client فقط)
    # -----------------------------------------------------------------

    def _start_health_monitor(self):
        def monitor():
            while not self._stop_flag.is_set():
                time.sleep(HOST_HEALTH_CHECK_INTERVAL)

                if self.state.role != "client":
                    continue

                try:
                    resp = httpx.get(
                        f"http://{self.state.host_ip}:{self.state.host_port}/health",
                        timeout=3,
                    )
                    resp.raise_for_status()
                except Exception:
                    # الـ Host لم يعد متاحاً محلياً — نبحث عن Host جديد على الشبكة
                    self.state.is_connected = False
                    self.state.role = "standalone"
                    self.state._notify()

                    self._discover_existing_host()
                    if self._discovered_host:
                        ip, port, host_hwid = self._discovered_host
                        self._try_become_client(ip, port, host_hwid)
                    else:
                        self._become_host()

        self._health_thread = threading.Thread(target=monitor, daemon=True)
        self._health_thread.start()

    # -----------------------------------------------------------------
    # اتصال يدوي احتياطي (في حال حجب mDNS بالشبكة)
    # -----------------------------------------------------------------

    def connect_manually(self, host_ip: str, host_port: int = 8001) -> dict:
        """
        يُستخدم كخيار احتياطي إذا فشل الاكتشاف التلقائي. يتطلب معرفة
        hwid الجهاز المضيف مسبقاً (يُعرض لصاحب الجهاز المضيف بواجهته
        الخاصة). يمر بنفس تحقق Railway الإلزامي — لا اختصار هنا أيضاً.
        """
        try:
            health_resp = httpx.get(f"http://{host_ip}:{host_port}/health", timeout=3)
            health_resp.raise_for_status()
            host_hwid = health_resp.json().get("hwid")
        except Exception:
            return {"success": False, "error": "تعذّر الوصول للجهاز على هذا العنوان"}

        if not host_hwid:
            return {"success": False, "error": "الجهاز المستهدف لا يعرض هويته (نسخة غير متوافقة؟)"}

        self._try_become_client(host_ip, host_port, host_hwid)
        if self.state.role == "client":
            return {"success": True}
        return {"success": False, "error": self.state.rejection_reason or "تم الرفض"}
