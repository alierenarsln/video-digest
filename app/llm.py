"""Model çağrıları — sağlayıcıdan bağımsız.

İki sağlayıcı:
  anthropic : Claude. Kalitesi daha yüksek (özellikle eleştirmen geçişi gibi
              "ne eksik" tipi akıl yürütmede) ve 1M bağlam. Anahtar gerekir.
  groq      : openai/gpt-oss-120b. Ücretsiz, Whisper ile aynı anahtar. 131k bağlam.
              Katı JSON şeması destekliyor (ÖLÇÜLDÜ; llama-3.3-70b desteklemiyor,
              o yüzden model seçimi serbest bırakılmadı).

Her iki yol da şemaya uyması garantili JSON döndürür — çağıran taraf farkı görmez.
"""

import asyncio
import json
import re
from contextvars import ContextVar
from typing import Any

import httpx

from .config import (
    ANTHROPIC_API_KEY,
    PROVIDER_WINDOWS,
    GEMINI_API_KEY,
    GEMINI_BASE_URL,
    GEMINI_MAX_OUTPUT,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_CONCURRENCY,
    GROQ_LLM_MODEL,
    GROQ_TPM,
    LLM_PROVIDER,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MAX_OUTPUT,
    OPENROUTER_MODEL,
    OUTPUT_LANGUAGE,
    SUMMARY_MODEL,
)


class LLMError(RuntimeError):
    pass


# Sağlayıcı artık iş başına seçilebiliyor (arayüzden), bu yüzden import anında
# sabitlenemez. ContextVar kullanıyoruz: worker her işin başında set_provider()
# çağırıyor, boru hattının derinindeki modüller parametre taşımadan doğru
# sağlayıcıyı görüyor.
_provider: ContextVar[str] = ContextVar("llm_provider", default=LLM_PROVIDER)


def set_provider(name: str) -> None:
    if name not in PROVIDER_WINDOWS:
        raise LLMError(f"Bilinmeyen sağlayıcı: {name}")
    _provider.set(name)


def provider() -> str:
    return _provider.get()


def windows() -> dict[str, int]:
    """Aktif sağlayıcının pencere boyutları. Sağlayıcıya göre TERS yönde ayarlı —
    bkz. config.PROVIDER_WINDOWS."""
    return PROVIDER_WINDOWS[provider()]


def language_rule() -> str:
    """Her özet prompt'una eklenir.

    Olmadığında çıktı diller arası karışıyordu: prompt'lar Türkçe, kaynak
    İngilizce → başlıklar Türkçe, bölüm özetleri İngilizce çıkıyordu. Onarım
    bu kuralı KULLANMAZ; o transkripti düzeltir, çevirmez.
    """
    if OUTPUT_LANGUAGE:
        return (
            f"\n\nÇIKTI DİLİ — İSTİSNASIZ: Her şeyi {OUTPUT_LANGUAGE} yaz. Başlıklar, "
            f"özetler, maddeler, terim tanımları, hepsi. Kaynak başka dilde olsa bile "
            f"{OUTPUT_LANGUAGE} yaz; bu bir çeviri görevidir ve normaldir. Özel isimleri, "
            f"komutları ve yerleşik teknik terimleri orijinal haliyle bırak."
        )
    return "\n\nÇIKTI DİLİ: Kaynağın dilinde yaz, çeviri yapma."


# Groq'un dakikalık token kotası küresel: çağıranların kendi paralelliği (bölüm
# özetleri, eleştirmen, onarım pencereleri) kotayı anında doldurup herkesi 429'a
# sokuyor. Paralellik burada FAYDA DEĞİL ZARAR — tek kapıdan sırayla geçiriyoruz.
_groq_gate = asyncio.Semaphore(GROQ_CONCURRENCY)


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    # Başlık yoksa Groq süreyi mesaj metninde veriyor: "try again in 2.145s"
    match = re.search(r"try again in ([\d.]+)s", resp.text)
    if match:
        return float(match.group(1))
    return float(2**attempt)


async def _anthropic_json(
    system: str, user: str, schema: dict[str, Any], effort: str, max_tokens: int
) -> dict[str, Any]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY or None)
    # Uzun çıktı beklendiği için streaming: büyük max_tokens ile streaming olmayan
    # istekler HTTP zaman aşımına düşüyor.
    async with client.messages.stream(
        model=SUMMARY_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = await stream.get_final_message()

    if message.stop_reason == "refusal":
        raise LLMError("Model isteği reddetti.")
    if message.stop_reason == "max_tokens":
        raise LLMError("Yanıt max_tokens sınırına takıldı — girdi çok büyük.")

    return _loads("".join(b.text for b in message.content if b.type == "text"))


def est_tokens(text: str) -> int:
    """Kaba token tahmini. Türkçe İngilizceden daha çok token yediği için 3'e bölüyoruz."""
    return len(text) // 3


async def _groq_json(
    system: str, user: str, schema: dict[str, Any], effort: str, max_tokens: int
) -> dict[str, Any]:
    # Groq'un dakikalık token kotası (TPM) istek bütçesine max_tokens'ı DA sayıyor:
    # 2k girdi + max_tokens=16000 -> "Requested 18127" -> 413. Kotadan büyük tek bir
    # istek asla geçmez, beklemek de çözmez. Bu yüzden çıktı bütçesini kısıyoruz.
    budget = int(GROQ_TPM * 0.85)
    prompt_tokens = est_tokens(system) + est_tokens(user)
    allowed = budget - prompt_tokens

    if allowed < 400:
        raise LLMError(
            f"Girdi tek istek için çok büyük: ~{prompt_tokens} token, "
            f"{GROQ_LLM_MODEL} dakikalık kotası {GROQ_TPM}. "
            f"Bu adımın daha küçük parçalara bölünmesi gerekiyor."
        )

    # gpt-oss AKIL YÜRÜTEN bir model ve düşünme token'ları da max_tokens'tan yeniyor.
    # Bütçe kısıtlıyken "high" düşünme, bütçeyi JSON'a sıra gelmeden bitirip BOŞ
    # çıktı bırakıyor (Groq bunu json_validate_failed + failed_generation:"" olarak
    # döndürüyor). Bu yüzden kalan bütçenin TAMAMINI veriyoruz ve düşünmeyi kısıyoruz.
    ladder = ["medium", "low"] if effort == "high" else ["low"]

    last: str = ""
    async with httpx.AsyncClient(timeout=600) as client:
        for reasoning in ladder:
            payload = {
                "model": GROQ_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "out", "schema": schema, "strict": True},
                },
                "max_tokens": min(max_tokens, allowed),
                "reasoning_effort": reasoning,
            }

            for attempt in range(8):
                async with _groq_gate:
                    resp = await client.post(
                        f"{GROQ_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json=payload,
                    )
                if resp.status_code == 429:
                    # Kota doldu ama istek boyutu uygun — beklemek işe yarar.
                    # (Boyutu kotadan büyük istek 413 verir ve beklemek çözmez.)
                    await asyncio.sleep(min(_retry_after(resp, attempt) + 1, 65))
                    last = resp.text[:300]
                    continue
                if resp.status_code == 400 and "json_validate_failed" in resp.text:
                    # Düşünme bütçeyi yedi, JSON üretilemedi → daha az düşünmeyle dene.
                    last = "json_validate_failed (düşünme bütçeyi tüketti)"
                    break
                if resp.status_code != 200:
                    raise LLMError(f"Groq {resp.status_code}: {resp.text[:400]}")

                choice = resp.json()["choices"][0]
                if choice.get("finish_reason") == "length":
                    last = "finish_reason=length"
                    break
                return _loads(choice["message"]["content"])

    raise LLMError(f"Groq isteği başarısız ({GROQ_LLM_MODEL}, bütçe {allowed} token): {last}")


async def _openrouter_json(
    system: str, user: str, schema: dict[str, Any], effort: str, max_tokens: int
) -> dict[str, Any]:
    """OpenRouter ücretsiz modelleri.

    Groq'un tersi kısıt: token kotası değil, GÜNDE 50 İSTEK (kredi 0 iken; $10
    kredi alınırsa 1000/gün) ve dakikada 20. Bağlam 262k olduğu için istekler
    büyük olabilir — pencere boyutları config'de buna göre geniş tutuluyor.
    """
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "out", "schema": schema, "strict": True},
        },
        # Çağıranlar Anthropic'e göre cömert değerler veriyor (16000); hy3 o
        # değerlerde JSON'u BOŞ döndürüyor (ölçüldü). Bütçeyi sabitliyoruz.
        "max_tokens": min(max_tokens, OPENROUTER_MAX_OUTPUT),
    }

    last = ""
    async with httpx.AsyncClient(timeout=600) as client:
        for attempt in range(5):
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json=payload,
            )
            if resp.status_code == 429:
                # Dakikalık sınır (20/dk) beklemekle geçer; GÜNLÜK sınır (50/gün)
                # geçmez — mesajı olduğu gibi taşıyoruz ki ayırt edilebilsin.
                await asyncio.sleep(min(_retry_after(resp, attempt) + 1, 65))
                last = resp.text[:300]
                continue
            if resp.status_code != 200:
                raise LLMError(f"OpenRouter {resp.status_code}: {resp.text[:400]}")

            data = resp.json()
            if "error" in data:
                raise LLMError(f"OpenRouter: {str(data['error'])[:300]}")
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LLMError("Yanıt kesildi — max_tokens yetmedi.")

            # Ücretsiz model çağrıların ~1/3'ünde BOŞ ya da yarım JSON döndürüyor
            # (ölçüldü). Segment bunu yakalayıp fallback yapıyordu ama summarize
            # ölümcül düşüyordu — tek flake bütün işi çöpe atıyordu. Boş/bozuk
            # çıktı geçici sayılıp retry ediliyor; ~1/3 flake'te 5 denemenin
            # hepsinin boş gelmesi ~%0.4. Her retry 50/gün bütçesinden yer.
            content = (choice["message"].get("content") or "").strip()
            if content:
                try:
                    return _loads(content)
                except LLMError:
                    last = f"bozuk JSON: {content[:120]}"
            else:
                last = "boş içerik (ücretsiz-model flake)"
            await asyncio.sleep(1)
            continue

    raise LLMError(
        f"OpenRouter {OPENROUTER_MODEL}: {last or 'kota/flake'}. Ücretsiz katman "
        f"günde 50 istek ve JSON'da ara sıra boş dönüyor; güvenilir çıktı için "
        f"Groq (LLM_PROVIDER=groq)."
    )


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema -> Gemini responseSchema (OpenAPI 3 alt kümesi).

    Gemini `additionalProperties`, `$schema`, `strict` gibi anahtarları
    reddediyor; yalnızca type/properties/items/enum/required/description
    tutulur. Özyinelemeli temizlik.
    """
    if not isinstance(schema, dict):
        return schema
    izinli = {"type", "properties", "items", "enum", "required", "description", "nullable"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in izinli:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {ad: _gemini_schema(alt) for ad, alt in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


async def _gemini_json(
    system: str, user: str, schema: dict[str, Any], effort: str, max_tokens: int
) -> dict[str, Any]:
    """Google Gemini — native yapısal çıktı (responseSchema).

    Kıyasta bu iş için seçilen: gemma'nın flake'i yok (şema API'de zorlanıyor),
    Türkçe iyi, 1M bağlam, ucuz. Groq gibi dakikalık token kotası derdi yok.
    """
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
            "maxOutputTokens": min(max_tokens, GEMINI_MAX_OUTPUT),
        },
    }
    url = f"{GEMINI_BASE_URL}/models/{GEMINI_MODEL}:generateContent"

    last = ""
    async with httpx.AsyncClient(timeout=600) as client:
        for attempt in range(5):
            resp = await client.post(
                url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload
            )
            if resp.status_code == 429:
                await asyncio.sleep(min(_retry_after(resp, attempt) + 1, 65))
                last = resp.text[:300]
                continue
            if resp.status_code != 200:
                raise LLMError(f"Gemini {resp.status_code}: {resp.text[:400]}")

            data = resp.json()
            adaylar = data.get("candidates") or []
            if not adaylar:
                # promptFeedback.blockReason gelebilir (güvenlik) — retry çözmez.
                geri = data.get("promptFeedback", {})
                raise LLMError(f"Gemini aday döndürmedi: {str(geri)[:200]}")
            aday = adaylar[0]
            # maxOutputTokens'a takıldıysa çıktı yarım — retry ile büyümez.
            if aday.get("finishReason") == "MAX_TOKENS":
                raise LLMError("Gemini yanıtı kesildi — maxOutputTokens yetmedi.")
            parts = (aday.get("content") or {}).get("parts") or []
            metin = "".join(p.get("text", "") for p in parts).strip()
            if metin:
                try:
                    return _loads(metin)
                except LLMError:
                    last = f"bozuk JSON: {metin[:120]}"
            else:
                last = "boş içerik"
            await asyncio.sleep(1)
            continue

    raise LLMError(f"Gemini {GEMINI_MODEL}: {last or 'kota/flake'}.")


def _loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Ücretsiz modeller (gemma) json_schema'ya rağmen çıktıyı markdown ``` ```
    # blokuna sarabiliyor ya da önüne açıklama koyabiliyor. Yapısal çıktının
    # bozulması geçici bir flake — ilk '{' ile son '}' arasını kurtarmayı dene.
    ilk, son = text.find("{"), text.rfind("}")
    if ilk != -1 and son > ilk:
        try:
            return json.loads(text[ilk : son + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Model geçerli JSON döndürmedi: {text[:80]!r}")


async def complete_json(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    effort: str = "high",
    max_tokens: int = 32000,
) -> dict[str, Any]:
    """Şemaya uyması garantili JSON döndürür. Sağlayıcı iş başına seçilir."""
    aktif = provider()
    if aktif == "anthropic":
        return await _anthropic_json(system, user, schema, effort, max_tokens)
    if aktif == "groq":
        return await _groq_json(system, user, schema, effort, max_tokens)
    if aktif == "openrouter":
        if not OPENROUTER_API_KEY:
            raise LLMError("Sağlayıcı openrouter seçildi ama OPENROUTER_API_KEY boş.")
        return await _openrouter_json(system, user, schema, effort, max_tokens)
    if aktif == "gemini":
        if not GEMINI_API_KEY:
            raise LLMError("Sağlayıcı gemini seçildi ama GEMINI_API_KEY boş.")
        return await _gemini_json(system, user, schema, effort, max_tokens)
    raise LLMError(f"Bilinmeyen sağlayıcı: {aktif}")
