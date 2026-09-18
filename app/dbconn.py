"""Veritabanı bağlantısı — iki modlu (SQLite | Postgres).

DATABASE_URL postgres ise Postgres (Vercel + Neon), yoksa bugünkü SQLite (DB_PATH,
Coolify volume'u). Sorgular SQLite sözdiziminde yazılır (`?` yer tutucu); Postgres
modunda `%s`'e çevrilir. Satırlar iki modda da r["kolon"] ve dict(r) ile okunur.

Neden iki mod: Vercel sunucusuz, kalıcı disk yok — SQLite dosyası her çağrıda
kaybolurdu. Coolify'da SQLite sorunsuz; geçiş bitene kadar ikisi yan yana yaşar.
"""

import sqlite3

from .config import DATABASE_URL, DB_PATH

IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# Zaman damgası sütun tipi. PG'nin REAL'i 4 baytlık float: unix zamanında (~1.8e9)
# yalnız ~7 anlamlı basamak → saniyeler ~2 dakikalık adımlara yuvarlanır.
FLOAT = "DOUBLE PRECISION" if IS_PG else "REAL"


class _PgConn:
    """sqlite3.Connection'ın bizim kullandığımız kadarını taklit eder:
    `with connect() as c: c.execute(sql, params).fetchone()/.fetchall()/iter`."""

    def __init__(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        # prepare_threshold=None: Neon'un havuzlu (PgBouncer, transaction modu)
        # adresinde sunucu tarafı hazır ifadeler bağlantılar arası kaybolur.
        self._c = psycopg.connect(
            DATABASE_URL, row_factory=dict_row, prepare_threshold=None
        )

    def execute(self, sql: str, params: tuple = ()):
        # Sorgularımızda literal '?' ya da '%' yok; yer tutucu çevirisi güvenli.
        return self._c.execute(sql.replace("?", "%s"), params)

    def __enter__(self) -> "_PgConn":
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        try:
            if exc_type is None:
                self._c.commit()
            else:
                self._c.rollback()
        finally:
            self._c.close()


_INIT_LOCK = 724_001  # rastgele sabit; yalnız şema kurulumunu sıraya sokar


def init_lock(conn) -> None:
    """Şema kurulum/tohum işlemini sıraya sok (yalnız Postgres).

    Vercel boş veritabanında aynı anda birkaç örnek açabilir; hepsi birden
    CREATE TABLE IF NOT EXISTS + auth tohumu koşarsa Postgres'te yarış olur (pg_type
    benzersizlik ihlali / id=1 çift INSERT) ve kaybeden örnek 500 verir. Kilit
    işlem (transaction) sonunda kendiliğinden bırakılır. SQLite tek süreç: gerekmez.
    """
    if IS_PG:
        conn.execute("SELECT pg_advisory_xact_lock(%s)" % _INIT_LOCK)


def connect():
    if IS_PG:
        return _PgConn()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
