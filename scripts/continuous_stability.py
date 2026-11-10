#!/usr/bin/env python3
"""Avaliação de estabilidade sustentada para fontes HLS.

O teste baixa segmentos completos em sequência durante uma janela limitada.
Não decodifica vídeo nem garante equivalência perfeita com o player CarTV.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import urljoin

import requests

USER_AGENT = "Mozilla/5.0 (compatible; PlaylistContinuousStability/1.0)"
MIN_SEGMENTS = 5


def _get(url: str, headers: dict[str, str], timeout: float):
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, **headers},
        timeout=(5, max(2, timeout)),
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def _media_playlist(url: str, headers: dict[str, str], timeout: float):
    response = _get(url, headers, timeout)
    body = response.text
    final_url = response.url
    response.close()
    rows = [line.strip() for line in body.splitlines() if line.strip()]
    if not rows or rows[0] != "#EXTM3U":
        raise ValueError("resposta não é M3U8")
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in rows):
        variants = []
        for index, line in enumerate(rows):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            attrs = line.split(":", 1)[1]
            bandwidth = re.search(r"(?:AVERAGE-BANDWIDTH|BANDWIDTH)=(\d+)", attrs, re.I)
            resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs, re.I)
            next_uri = next((x for x in rows[index + 1:] if not x.startswith("#")), None)
            if next_uri:
                pixels = int(resolution.group(1)) * int(resolution.group(2)) if resolution else 0
                rate = int(bandwidth.group(1)) if bandwidth else 0
                variants.append((pixels, rate, urljoin(final_url, next_uri)))
        if not variants:
            raise ValueError("manifesto master sem variantes")
        # Estressa a variante de maior resolução/bitrate anunciada.
        variants.sort(reverse=True)
        child = _get(variants[0][2], headers, timeout)
        child_url, child_text = child.url, child.text
        child.close()
        rows = [line.strip() for line in child_text.splitlines() if line.strip()]
        if not rows or rows[0] != "#EXTM3U" or any(x.startswith("#EXT-X-STREAM-INF:") for x in rows):
            raise ValueError("variante não contém playlist de mídia")
        final_url = child_url

    target = 6.0
    for line in rows:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = max(1.0, float(line.split(":", 1)[1]))
            except ValueError:
                pass

    sequence = 0
    for line in rows:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
            break

    segments = []
    duration = None
    for line in rows:
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = None
        elif not line.startswith("#") and duration is not None:
            segments.append((sequence + len(segments), urljoin(final_url, line), duration))
            duration = None
    return segments, target, "#EXT-X-ENDLIST" in rows


def test_continuous(url: str, headers: dict[str, str], duration_seconds: int = 120,
                    timeout: int = 12) -> dict:
    """Baixa segmentos HLS completos em sequência durante a janela solicitada."""
    started = time.monotonic()
    deadline = started + duration_seconds
    seen: set[tuple[int, str]] = set()
    samples = []
    failures = 0
    last_target = 6.0
    ended = False
    last_error = ""

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            segments, last_target, ended = _media_playlist(url, headers, min(timeout, remaining))
            new_segments = [item for item in segments if (item[0], item[1]) not in seen]
            for sequence, segment_url, segment_duration in new_segments:
                if time.monotonic() >= deadline:
                    break
                seen.add((sequence, segment_url))
                before = time.monotonic()
                try:
                    response = _get(segment_url, headers, min(timeout, max(2, deadline - before)))
                    payload = response.content
                    response.close()
                    elapsed = max(0.001, time.monotonic() - before)
                    if not payload:
                        raise ValueError("segmento vazio")
                    samples.append({
                        "duration": max(0.1, segment_duration),
                        "download": elapsed,
                        "bytes": len(payload),
                    })
                except Exception as exc:
                    failures += 1
                    last_error = type(exc).__name__
            if ended and not new_segments:
                last_error = "playlist VOD terminou; sem janela ao vivo contínua"
                break
        except Exception as exc:
            failures += 1
            last_error = type(exc).__name__

        remaining = deadline - time.monotonic()
        if remaining > 0:
            # Recarrega a playlist aproximadamente a cada 1/3 do target duration.
            time.sleep(min(1.0, max(0.25, last_target / 3.0), remaining))

    elapsed_total = time.monotonic() - started
    count = len(samples)
    if count:
        ratios = [sample["download"] / sample["duration"] for sample in samples]
        timely = sum(ratio <= 0.80 for ratio in ratios) / count
        mean = sum(ratios) / count
        variance = sum((value - mean) ** 2 for value in ratios) / count
        cv = math.sqrt(variance) / mean if mean > 0 else 1.0
        regularity = max(0.0, 1.0 - min(1.0, cv))
        median_headroom = sorted(1.0 / max(0.001, ratio) for ratio in ratios)[count // 2]
        headroom = min(1.0, median_headroom / 2.0)
        completed_ratio = count / (count + failures) if count + failures else 0.0
        score = round(100 * (0.40 * timely + 0.30 * regularity +
                             0.20 * completed_ratio + 0.10 * headroom))
        deadline_misses = sum(ratio > 1.0 for ratio in ratios)
        avg_download = sum(sample["download"] for sample in samples) / count
        max_download = max(sample["download"] for sample in samples)
        total_bytes = sum(sample["bytes"] for sample in samples)
    else:
        timely = regularity = headroom = completed_ratio = 0.0
        score = 0
        deadline_misses = 0
        avg_download = max_download = 0.0
        total_bytes = 0

    valid = count >= MIN_SEGMENTS and elapsed_total >= min(30, duration_seconds * 0.5)
    successful = valid and failures == 0 and score >= 75
    return {
        "score": score if valid else 0,
        "valid": valid,
        "successful": successful,
        "duration_seconds": round(elapsed_total, 1),
        "segments": count,
        "failures": failures,
        "deadline_misses": deadline_misses,
        "avg_download": round(avg_download, 3),
        "max_download": round(max_download, 3),
        "timely_ratio": round(timely, 3),
        "regularity": round(regularity, 3),
        "headroom": round(headroom, 3),
        "bytes": total_bytes,
        "last_error": last_error,
    }
