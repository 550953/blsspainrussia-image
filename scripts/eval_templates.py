#!/usr/bin/env python3
"""Серия live-запусков email_template с локальной сводкой; HTML не печатается в stdout."""
import argparse
import concurrent.futures
import html
import json
import os
import re
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))], 2)


def text_5grams(document):
    words = re.findall(r"\w+", html.unescape(re.sub(r"<[^>]*>", " ", document)).casefold())
    return {tuple(words[i:i + 5]) for i in range(max(0, len(words) - 4))}


def jaccard(first, second):
    union = first | second
    return len(first & second) / len(union) if union else 1.0


def was_autofix_rescued(attempts):
    """True when a raw validation failure became error-free after a reported autofix."""
    return any(
        attempt.get("raw_errors") and not attempt.get("errors") and attempt.get("autofixes")
        for attempt in attempts
    )


# Копия prompt.FORBIDDEN_STEMS: скрипт самостоятельный и не импортирует код сервиса (тест следит за совпадением).
FORBIDDEN_STEMS = (
    "с уважением", "с теплом", "рады вас приветствовать", "что происходит", "путь вашего заказа", "как продвигается",
    "прямо сейчас", "персональн", "под контролем", "бережн", "максимум внимания", "накладн", "служб", "специалист",
    "отдел", "мастер", "склад", "курьер", "трек",
)


def visible_text(document):
    return html.unescape(re.sub(r"<[^>]*>", " ", re.sub(r"<(?:style|script)\b[\s\S]*?</(?:style|script)>", " ", document or "", flags=re.I)))


def extract_texts(document):
    """Приветствие (h1), заголовок блока (первый h2) и подпись (последний курсивный абзац)."""
    def clean(fragment):
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", fragment))).strip()
    h1 = re.search(r"<h1\b[^>]*>([\s\S]*?)</h1>", document or "", re.I)
    h2 = re.search(r"<h2\b[^>]*>([\s\S]*?)</h2>", document or "", re.I)
    italic = re.findall(r"<p\b[^>]*font-style\s*:\s*italic[^>]*>([\s\S]*?)</p>", document or "", re.I)
    return {"greeting": clean(h1.group(1)) if h1 else "", "h2": clean(h2.group(1)) if h2 else "", "signoff": clean(italic[-1]) if italic else ""}


def count_forbidden(document):
    """Сколько запрещённых оборотов (по основам слов) в видимом тексте: Counter {основа: число}."""
    text = re.sub(r"\s+", " ", visible_text(document)).casefold().replace("ё", "е")
    return Counter({stem: text.count(stem) for stem in FORBIDDEN_STEMS if stem in text})


def evaluate_one(args, number, out_dir):
    headers = {"X-API-Key": args.api_key}
    base = args.base_url.rstrip("/")
    body = {
        "account_id": args.account_id,
        "client_request_id": "eval-" + uuid.uuid4().hex,
        "seed": args.seed + number,
        "max_attempts": args.max_attempts,
    }
    if args.model:
        body["model"] = args.model
    with httpx.Client(timeout=args.timeout) as client:
        started = time.monotonic()
        response = client.post(base + "/template/submit", headers=headers, json=body)
        response.raise_for_status()
        accepted = response.json()
        job_id = accepted["job_id"]
        while time.monotonic() - started < args.deadline:
            result = client.get(
                base + "/template/result/" + job_id,
                headers=headers,
                params={"html": 1, "attempts_html": 1} if args.attempts_html else {"html": 1},
            )
            result.raise_for_status()
            payload = result.json()
            if payload["status"] in {"done", "error"}:
                break
            time.sleep((payload.get("poll_after_ms") or args.poll_interval * 1000) / 1000)
        else:
            return {
                "job_id": job_id, "status": "client_timeout",
                "total_seconds": round(time.monotonic() - started, 2),
            }
    total = time.monotonic() - started
    template = payload.get("template") or {}
    document = template.get("html") or ""
    filename = ""
    if document:
        filename = f"{number:03d}-{job_id}.html"
        (out_dir / filename).write_text(document, encoding="utf-8")
    attempts = template.get("attempts") or []
    texts = extract_texts(document)
    forbidden_final = count_forbidden(document)
    # До автофикса: только если сервер запущен с TEMPLATE_KEEP_ATTEMPT_HTML=1 и скрипт с --attempts-html.
    raw_documents = [item.get("raw_html") or "" for item in (payload.get("attempts_html") or [])]
    forbidden_raw = count_forbidden(raw_documents[0]) if raw_documents else None
    warnings_report = (template.get("report") or {}).get("warnings") or []
    first_valid = bool(attempts and not attempts[0].get("errors"))
    raw_rules = Counter(rule for attempt in attempts for rule in attempt.get("raw_errors", []))
    tokens = Counter()
    for attempt in attempts:
        usage = attempt.get("tokens") or {}
        for key in ("in", "out", "thoughts", "cached"):
            tokens[key] += int(usage.get(key, 0) or 0)
    brief = accepted.get("brief") or {}
    return {
        "job_id": job_id,
        "status": payload["status"],
        "error": payload.get("error"),
        "generation_error": template.get("generation_error"),
        "valid": bool(template.get("valid")),
        "first_attempt_valid": first_valid,
        "attempts": len(attempts),
        "autofix_rescued": was_autofix_rescued(attempts),
        "total_seconds": round(total, 2),
        "model": template.get("model"),
        "palette": brief.get("palette_family"),
        "currency_method": brief.get("currency_method"),
        "bytes": len(document.encode("utf-8")),
        "html_file": filename,
        "raw_rule_counts": dict(raw_rules),
        "greeting": texts["greeting"], "h2": texts["h2"], "signoff": texts["signoff"],
        "forbidden_final": dict(forbidden_final),
        "forbidden_before_autofix": dict(forbidden_raw) if forbidden_raw is not None else None,
        "warning_lines": len(warnings_report),
        "warning_rules": dict(Counter(str(item.get("rule")) for item in warnings_report)),
        "had_a06": any("A06" in (attempt.get("raw_errors") or []) for attempt in attempts),
        "first_attempt_error_details": (attempts[0].get("raw_error_details") or [])[:10] if attempts and not first_valid else [],
        "tokens": dict(tokens),
        "text_5grams": text_5grams(document),
        "colors": sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", document))),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("TEMPLATE_BASE_URL"))
    parser.add_argument("--api-key", default=os.getenv("TEMPLATE_API_KEY"))
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--out", default="eval_out")
    parser.add_argument("--account-id", default="template-eval")
    # --seed-base: одинаковые брифы для двух моделей (seed письма = база + номер), сравнение честное.
    parser.add_argument("--seed", "--seed-base", dest="seed", type=int, default=20261013)
    parser.add_argument("--attempts-html", action="store_true", help="забирать HTML попыток (сервер с TEMPLATE_KEEP_ATTEMPT_HTML=1), чтобы считать запрещённые обороты до автофикса")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--deadline", type=float, default=900.0)
    args = parser.parse_args()
    if not args.base_url:
        parser.error("Укажите --base-url или задайте TEMPLATE_BASE_URL")
    if not args.api_key:
        parser.error("Укажите --api-key или задайте TEMPLATE_API_KEY")
    args.n = max(1, args.n)
    args.concurrency = max(1, args.concurrency)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(evaluate_one, args, number, out_dir) for number in range(1, args.n + 1)]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"status": "client_error", "error_type": type(exc).__name__})
                print(f"request failed: {type(exc).__name__}")
    completed = [item for item in results if item.get("status") == "done"]
    valid = [item for item in completed if item.get("valid")]
    durations = [item["total_seconds"] for item in results if "total_seconds" in item]
    raw_rules = Counter()
    for item in results:
        raw_rules.update(item.get("raw_rule_counts", {}))
    error_codes = Counter()
    for item in results:
        code = item.get("error") or item.get("generation_error")
        if not code and item.get("status") != "done":
            code = item.get("status", "unknown")
        if code:
            error_codes[str(code)] += 1
    grams = [(item["job_id"], item["text_5grams"]) for item in results if item.get("text_5grams")]
    similar_pairs = []
    for i, (first_id, first) in enumerate(grams):
        for second_id, second in grams[i + 1:]:
            score = jaccard(first, second)
            if score > 0.5:
                similar_pairs.append({"job_ids": [first_id, second_id], "jaccard": round(score, 3)})
    summary = {
        "requested": args.n,
        "completed": len(completed),
        "errors_by_code": dict(error_codes),
        "first_attempt_valid_rate": (
            sum(bool(item.get("first_attempt_valid")) for item in completed) / len(completed)
            if completed else None
        ),
        "final_valid_rate": len(valid) / len(completed) if completed else None,
        "attempt_distribution": dict(Counter(str(item.get("attempts", 0)) for item in completed)),
        "autofix_rescued": sum(bool(item.get("autofix_rescued")) for item in completed),
        "autofix_rescued_rate": (
            sum(bool(item.get("autofix_rescued")) for item in completed) / len(completed)
            if completed else None
        ),
        "top_raw_rules": raw_rules.most_common(10),
        "diversity": {
            "distinct_greetings": len({item["greeting"] for item in completed if item.get("greeting")}),
            "distinct_h2": len({item["h2"] for item in completed if item.get("h2")}),
            "distinct_signoffs": len({item["signoff"] for item in completed if item.get("signoff")}),
            "distinct_palettes": len({item.get("palette") for item in completed if item.get("palette")}),
            "letters": len(completed),
        },
        "forbidden_in_final": sum(sum((item.get("forbidden_final") or {}).values()) for item in completed),
        "forbidden_in_final_by_stem": Counter(
            {stem: sum((item.get("forbidden_final") or {}).get(stem, 0) for item in completed) for stem in FORBIDDEN_STEMS}
        ).most_common(10),
        "forbidden_before_autofix": (
            sum(sum((item.get("forbidden_before_autofix") or {}).values()) for item in completed)
            if any(item.get("forbidden_before_autofix") is not None for item in completed) else None
        ),
        "letters_with_a06": sum(bool(item.get("had_a06")) for item in completed),
        "warning_lines_per_letter": (
            round(sum(item.get("warning_lines", 0) for item in completed) / len(completed), 2) if completed else None
        ),
        "first_pass_failures": [
            {"job_id": item["job_id"], "details": item["first_attempt_error_details"]}
            for item in completed if item.get("first_attempt_error_details")
        ],
        "latency_seconds": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95)},
        "tokens": {
            key: sum((item.get("tokens") or {}).get(key, 0) for item in completed)
            for key in ("in", "out", "thoughts", "cached")
        },
        "palette_count": len({item.get("palette") for item in completed if item.get("palette")}),
        "top_palettes": Counter(item.get("palette") for item in completed if item.get("palette")).most_common(3),
        "distinct_hex_palettes": len({
            tuple(item.get("colors", [])) for item in completed if item.get("colors")
        }),
        "top_hex_palettes": Counter(
            tuple(item.get("colors", [])) for item in completed if item.get("colors")
        ).most_common(3),
        "currency_methods": dict(Counter(str(item.get("currency_method")) for item in completed)),
        "top_hex_colors": Counter(color for item in completed for color in item.get("colors", [])).most_common(10),
        "similar_5gram_pairs_over_0_5": similar_pairs,
        "results": [{key: value for key, value in item.items() if key != "text_5grams"} for item in results],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key not in {"results"}}, ensure_ascii=False, indent=2))
    print(f"Saved summary: {summary_path}")


if __name__ == "__main__":
    main()
