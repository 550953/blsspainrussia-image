# ===========================================================================
# ДОПИШИ ЭТОТ БЛОК В САМЫЙ КОНЕЦ файла ocr_service/gemini_sheet.py
# (после последней существующей функции)
# ===========================================================================

CAPTCHA_SINGLE_TIMEOUT = float(os.getenv("CAPTCHA_SINGLE_TIMEOUT", "25"))
CAPTCHA_SINGLE_MAX_CALLS = int(os.getenv("CAPTCHA_SINGLE_MAX_CALLS", "6"))


async def recognize_single_image(image_bytes: bytes, prompt: str) -> Optional[str]:
    """Один Gemini-вызов на ОДНУ картинку (без contact sheet), тот же пул ключей и каналов.

    Возвращает сырой текст ответа или None, если ключей нет / все попытки исчерпаны.
    Ожидание свободного ключа ограничено дедлайном, а не числом итераций: под нагрузкой
    запрос не должен падать только потому, что все ключи на секунду заняты."""
    if not GEMINI_KEYS or gemini_model_name is None:
        return None

    deadline = time.monotonic() + CAPTCHA_SINGLE_TIMEOUT
    calls = 0
    last_error: Optional[str] = None

    while calls < CAPTCHA_SINGLE_MAX_CALLS and time.monotonic() < deadline:
        idx, key_name = gemini_pool.acquire()
        if idx is None:
            groups = gemini_pool.grouped_keys()
            if not groups["ready"] and not groups["in_use"] and not groups["cooldown"]:
                break  # все ключи невалидны или на billing/project hold
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        calls += 1
        proxy_url = smart_proxy_pool.next()
        t0 = time.monotonic()
        try:
            text, _meta = await _call_gemini_rest(
                image_bytes,
                prompt,
                gemini_model_name,
                gemini_pool.keys[idx],
                proxy_url,
                # С запасом: у thinking-моделей токены рассуждений тоже идут в лимит.
                generation_config={"temperature": 0.0, "maxOutputTokens": 256},
            )
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, None)
            if text:
                _job_log(
                    "captcha6_attempt", key_profile=key_name, proxy=_proxy_label(proxy_url),
                    attempt=calls, latency_ms=round((time.monotonic() - t0) * 1000),
                )
                return text
            last_error = "empty response"
        except ProxyUnavailable as exc:
            last_error = f"{type(exc).__name__}: {_safe_gemini_message(str(exc))}"
            smart_proxy_pool.mark_failed(proxy_url, exc)
            gemini_pool.release(idx, None)
        except GeminiAPIError as exc:
            last_error = str(exc)
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {_safe_gemini_message(str(exc))}"
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, GeminiAPIError(0, "CLIENT_RESPONSE_ERROR", last_error, {
                "category": "MODEL_OR_RESPONSE_ERROR",
                "cooldown_seconds": 15.0,
                "permanent_key_failure": False,
                "hold_until_restart": False,
                "action": "temporary_key_pause; proxy is healthy",
            }))

    print(f"[pid={os.getpid()}][captcha6] Gemini не ответил: {last_error or 'нет доступного ключа/канала'}")
    return None
