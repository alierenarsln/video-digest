"""Log çıktısını UTF-8'e sabitler.

Windows konsolu varsayılan olarak cp1254 kullanıyor; Türkçe log'lar ve özel
karakterler UnicodeEncodeError atıp worker'ı düşürüyordu. Docker'da (UTF-8)
bu sorun görünmediği için yerel koşuya kadar fark edilmez.
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
