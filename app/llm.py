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
from typing import Any

import httpx

from .config import (
    ANTHROPIC_API_KEY,
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
            return _loads(choice["message"]["content"])

    raise LLMError(
        f"OpenRouter kotası doldu ({OPENROUTER_MODEL}). Ücretsiz katman günde 50 "
        f"istek; bir video ~12-15 istek yiyor. Groq'a dönmek için LLM_PROVIDER=groq. "
        f"Ayrıntı: {last}"
    )


def _loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # yapısal çıktı ile beklenmez
        raise LLMError(f"Model geçerli JSON döndürmedi: {exc}") from exc


async def complete_json(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    effort: str = "high",
    max_tokens: int = 32000,
) -> dict[str, Any]:
    """Şemaya uyması garantili JSON döndürür."""
    if LLM_PROVIDER == "anthropic":
        return await _anthropic_json(system, user, schema, effort, max_tokens)
    if LLM_PROVIDER == "groq":
        return await _groq_json(system, user, schema, effort, max_tokens)
    if LLM_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY:
            raise LLMError("LLM_PROVIDER=openrouter ama OPENROUTER_API_KEY boş.")
        return await _openrouter_json(system, user, schema, effort, max_tokens)
    raise LLMError(f"Bilinmeyen LLM_PROVIDER: {LLM_PROVIDER}")
