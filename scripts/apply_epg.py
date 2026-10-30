#!/usr/bin/env python3
"""Aplica IDs XMLTV à playlist otimizada usando correspondência exata de nomes."""
from __future__ import annotations
import re
import sys
import unicodedata
from collections import defaultdict
from html import unescape
from pathlib import Path
import xml.etree.ElementTree as ET
import requests

EPG_URL = "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/main/claro.xml"
USER_AGENT = "Mozilla/5.0 (compatible; PlaylistEPGMapper/1.0)"


def normalize(value: str) -> str:
    value = unescape(value or "").strip().casefold()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_xmltv_channels(xml_bytes: bytes) -> dict[str, set[str]]:
    by_name: dict[str, set[str]] = defaultdict(set)
    # iterparse evita construir a árvore completa do XMLTV na memória.
    import io
    for _, elem in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if elem.tag.rsplit("}", 1)[-1] == "channel":
            channel_id = (elem.attrib.get("id") or "").strip()
            if channel_id:
                for child in elem:
                    if child.tag.rsplit("}", 1)[-1] == "display-name" and child.text:
                        name = normalize(child.text)
                        if name:
                            by_name[name].add(channel_id)
            elem.clear()
    return by_name


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "playlist-otimizada.m3u")
    if not path.is_file():
        print(f"Playlist não encontrada: {path}", file=sys.stderr)
        return 2

    response = requests.get(EPG_URL, headers={"User-Agent": USER_AGENT}, timeout=(15, 90))
    response.raise_for_status()
    if not response.content.lstrip().startswith(b"<?xml") and b"<tv" not in response.content[:2048]:
        raise RuntimeError("A URL do EPG não retornou um XMLTV reconhecível.")
    channels = load_xmltv_channels(response.content)
    if not channels:
        raise RuntimeError("O XMLTV foi baixado, mas nenhum canal foi encontrado.")

    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if line.startswith("#EXTM3U")), None)
    if header_index is None:
        raise RuntimeError("Cabeçalho #EXTM3U não encontrado.")

    header = lines[header_index]
    header = re.sub(r'\s+x-tvg-url="[^"]*"', "", header, flags=re.I)
    header = re.sub(r'\s+url-tvg="[^"]*"', "", header, flags=re.I)
    lines[header_index] = header.rstrip() + f' x-tvg-url="{EPG_URL}"'

    mapped = 0
    ambiguous = 0
    unmatched = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line.split(",", 1)[0]))
        current_id = attrs.get("tvg-id", "").strip()
        if current_id:
            continue
        name = attrs.get("tvg-name", "")
        if not name:
            name = line.split(",", 1)[1].strip() if "," in line else ""
        key = normalize(name)
        matches = channels.get(key, set())
        if len(matches) == 1:
            channel_id = next(iter(matches))
            # Acrescenta tvg-id sem alterar os demais atributos ou o nome do canal.
            insertion = f' tvg-id="{channel_id}"'
            comma = line.find(",")
            if comma < 0:
                raise RuntimeError(f"EXTINF sem título separado por vírgula: {line}")
            lines[i] = line[:comma] + insertion + line[comma:]
            mapped += 1
        elif len(matches) > 1:
            ambiguous += 1
            unmatched.append(name + " (IDs XMLTV ambíguos)")
        else:
            unmatched.append(name)

    if mapped == 0:
        raise RuntimeError("Nenhum canal teve correspondência exata; playlist não será alterada.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    unique_unmatched = list(dict.fromkeys(unmatched))
    print(f"EPG: {EPG_URL}")
    print(f"Canais XMLTV: {len(channels)}")
    print(f"Entradas M3U associadas: {mapped}")
    print(f"Correspondências ambíguas: {ambiguous}")
    print(f"Nomes sem correspondência: {len(unique_unmatched)}")
    for name in unique_unmatched[:100]:
        print(f"SEM EPG: {name}")
    if len(unique_unmatched) > 100:
        print(f"... e mais {len(unique_unmatched) - 100} nomes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
