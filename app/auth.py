"""Oturum tabanlı kimlik doğrulama — tarayıcı popup'ı yerine gerçek login sayfası.

Şifre artık env'de değil, DB'de HASH'li (pbkdf2). Böylece kurtarma koduyla
değiştirilebiliyor. İlk açılışta env'deki APP_PASSWORD'den tohumlanır ve bir
KURTARMA KODU üretilip loga bir kez yazılır (Coolify loglarından alınır).

İki giriş yolu korunur:
  - Tarayıcı: imzalı oturum çerezi (login formu kurar; "beni hatırla" = uzun ömür).
  - Script/curl: HTTP Basic (indir-yukle.ps1, kaydet.ps1, upload komutları KIRILMASIN).

Hepsi Python stdlib — yeni bağımlılık yok.
"""

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from .config import APP_PASSWORD, APP_USER, DB_PATH

_ITER = 200_000
# Karışan karakterler yok (0/O, 1/I/l) — kurtarma kodu elle yazılabilir olsun.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COOKIE = "tanik_session"
_REMEMBER_AGE = 30 * 86400   # beni hatırla: 30 gün
_SESSION_AGE = 12 * 3600     # değilse: 12 saat (çerez de oturumluk)

_secret_cache: str | None = None


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _hash(value: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt_hex), _ITER).hex()


def _salt() -> str:
    return secrets.token_hex(16)


def _new_recovery() -> str:
    return "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)
    )


def init() -> str | None:
    """Auth tablosu + ilk tohum. İLK kez tohumlanırsa kurtarma kodunu (düz metin)
    döner (çağıran loga yazsın); sonraki açılışlarda None."""
    global _secret_cache
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS auth ("
            " id INTEGER PRIMARY KEY CHECK (id=1),"
            " pass_hash TEXT NOT NULL, pass_salt TEXT NOT NULL,"
            " recovery_hash TEXT NOT NULL, recovery_salt TEXT NOT NULL,"
            " secret TEXT NOT NULL)"
        )
        # Ek kullanıcılar (admin dışında) — çok-kullanıcı girişi. Kütüphane paylaşımlı
        # (iş başına kullanıcı yok); bu yalnızca ayrı giriş bilgisi verir.
        c.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " username TEXT PRIMARY KEY, pass_hash TEXT NOT NULL,"
            " pass_salt TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        row = c.execute("SELECT secret FROM auth WHERE id=1").fetchone()
        if row:
            _secret_cache = row["secret"]
            return None
        # Tohum: env şifresi + yeni kurtarma kodu + oturum imzalama sırrı.
        psalt, rsalt = _salt(), _salt()
        rcode = _new_recovery()
        _secret_cache = secrets.token_hex(32)
        c.execute(
            "INSERT INTO auth (id,pass_hash,pass_salt,recovery_hash,recovery_salt,secret)"
            " VALUES (1,?,?,?,?,?)",
            (_hash(APP_PASSWORD, psalt), psalt, _hash(rcode, rsalt), rsalt, _secret_cache),
        )
        return rcode


def is_open() -> bool:
    """Şifre boşsa (yerelde APP_PASSWORD tanımsız) koruma yok — eski davranış."""
    return not APP_PASSWORD


def _secret() -> str:
    global _secret_cache
    if _secret_cache is None:
        with _conn() as c:
            row = c.execute("SELECT secret FROM auth WHERE id=1").fetchone()
            _secret_cache = row["secret"] if row else secrets.token_hex(32)
    return _secret_cache


def verify_password(pw: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT pass_hash, pass_salt FROM auth WHERE id=1").fetchone()
    if not row:
        return False
    return hmac.compare_digest(_hash(pw, row["pass_salt"]), row["pass_hash"])


def verify_recovery(code: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT recovery_hash, recovery_salt FROM auth WHERE id=1").fetchone()
    if not row:
        return False
    # Boşluk/harf toleransı: kullanıcı elle yazarken küçük harf/boşluk koyabilir.
    temiz = code.strip().upper().replace(" ", "")
    return hmac.compare_digest(_hash(temiz, row["recovery_salt"]), row["recovery_hash"])


def set_password(new_pw: str) -> str:
    """Şifreyi değiştir VE kurtarma kodunu yenile (eskisi kullanıldı). Yeni
    kurtarma kodunu döner ki kullanıcıya bir kez gösterilsin."""
    psalt, rsalt = _salt(), _salt()
    rcode = _new_recovery()
    with _conn() as c:
        c.execute(
            "UPDATE auth SET pass_hash=?, pass_salt=?, recovery_hash=?, recovery_salt=? WHERE id=1",
            (_hash(new_pw, psalt), psalt, _hash(rcode, rsalt), rsalt),
        )
    return rcode


# --- ek kullanıcılar (çok-kullanıcı giriş; kütüphane PAYLAŞIMLI) ---
def add_user(username: str, pw: str) -> bool:
    u = (username or "").strip()
    if not u or not pw or u.lower() == (APP_USER or "").strip().lower():
        return False  # boş ya da admin ile çakışan ad reddedilir
    psalt = _salt()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO users (username,pass_hash,pass_salt,created_at)"
            " VALUES (?,?,?,?)",
            (u, _hash(pw, psalt), psalt, time.time()),
        )
    return True


def remove_user(username: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM users WHERE username=?", ((username or "").strip(),))


def list_users() -> list[str]:
    with _conn() as c:
        rows = c.execute("SELECT username FROM users ORDER BY created_at").fetchall()
    return [r["username"] for r in rows]


def verify_user(username: str, pw: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT pass_hash, pass_salt FROM users WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    if not row:
        return False
    return hmac.compare_digest(_hash(pw, row["pass_salt"]), row["pass_hash"])


def verify_login(username: str, pw: str) -> bool:
    """Admin (env APP_USER + auth tablosu) VEYA users tablosundaki ek kullanıcı."""
    u = (username or "").strip()
    if secrets.compare_digest(u, APP_USER) and verify_password(pw):
        return True
    return verify_user(u, pw)


# --- oturum çerezi (imzalı) ---
def make_token(remember: bool) -> tuple[str, int | None]:
    """(token, cookie_max_age) döner. max_age None = oturumluk çerez."""
    age = _REMEMBER_AGE if remember else _SESSION_AGE
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": APP_USER, "exp": int(time.time()) + age}).encode()
    ).decode()
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", (_REMEMBER_AGE if remember else None)


def verify_token(token: str) -> bool:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0)) > time.time()
    except Exception:
        return False


def verify_basic(header: str | None) -> bool:
    """Script/curl için HTTP Basic — DB'deki şifreye karşı doğrular."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return verify_login(user, pw)
