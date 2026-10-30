#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures, re, sys, time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
import requests

TIMEOUT = 12
WORKERS = 16
UA = "Mozilla/5.0 (compatible; PlaylistHealthCheck/1.0)"

@dataclass
class Entry:
    block: list[str]
    url: str
    key: str
    label: str
    quality: int
    headers: dict[str, str]
    order: int

@dataclass
class Check:
    entry: Entry
    ok: bool
    quality: int
    latency: int
    reason: str

def quality_of(s):
    dims = re.findall(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", s)
    if dims: return max(int(w)*int(h) for w,h in dims)
    t=s.upper()
    for token,score in (("4K",3840*2160),("UHD",3840*2160),("FHD",1920*1080),("1080",1920*1080),("HD",1280*720),("720",1280*720),("SD",854*480),("540",854*480),("480",854*480),("360",640*360)):
        if token in t: return score
    return 0

def parse(text):
    lines=[x.rstrip("\r") for x in text.splitlines() if x.strip()]
    header=next((x for x in lines if x.startswith("#EXTM3U")),"#EXTM3U")
    entries=[]; pending=[]
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending=[line]
        elif pending:
            pending.append(line)
            if line and not line.startswith("#"):
                info=pending[0]
                attrs=dict(re.findall(r'([\w-]+)="([^"]*)"',info.split(",",1)[0]))
                title=info.split(",",1)[1].strip() if "," in info else "Canal"
                raw=attrs.get("tvg-id") or attrs.get("tvg-name") or title
                key=re.sub(r"\s*\[[^\]]*(?:4K|UHD|FHD|HD|SD|\d{3,4}\s*[xX]\s*\d{3,4})[^\]]*\]\s*$","",raw,flags=re.I)
                key=re.sub(r"\s+"," ",key).strip().casefold() or title.casefold()
                headers={}
                for d in pending[1:-1]:
                    m=re.match(r"#EXTVLCOPT:([^=]+)=(.*)",d,re.I)
                    if m:
                        k,v=m.group(1).lower(),m.group(2).strip()
                        if k=="http-referrer": headers["Referer"]=v
                        elif k=="http-user-agent": headers["User-Agent"]=v
                entries.append(Entry(pending[:],line,key,title,quality_of(info),headers,len(entries)))
                pending=[]
    return header,entries

def get(url,headers,timeout):
    h={"User-Agent":UA,**headers}
    start=time.monotonic()
    r=requests.get(url,headers=h,timeout=timeout,allow_redirects=True)
    ms=int((time.monotonic()-start)*1000)
    r.raise_for_status()
    return r,ms

def check(e,timeout):
    try:
        r,lat=get(e.url,e.headers,timeout)
        body=r.text[:1_000_000]
        if "#EXTM3U" not in body[:4096]:
            return Check(e,len(r.content)>512,e.quality,lat,"resposta HTTP não-HLS")
        rows=[x.strip() for x in body.splitlines() if x.strip()]
        variants=[]
        for i,row in enumerate(rows):
            if row.startswith("#EXT-X-STREAM-INF:"):
                m=re.search(r"RESOLUTION=(\d+)x(\d+)",row,re.I)
                score=int(m.group(1))*int(m.group(2)) if m else 0
                nxt=next((x for x in rows[i+1:] if not x.startswith("#")),None)
                if nxt: variants.append((score,urljoin(r.url,nxt)))
        q=e.quality
        if variants:
            q,url=max(variants,key=lambda x:x[0])
            vr,more=get(url,e.headers,timeout); lat+=more
            if "#EXTM3U" not in vr.text[:4096]: return Check(e,False,0,lat,"variante HLS inválida")
            rows=[x.strip() for x in vr.text[:1_000_000].splitlines() if x.strip()]
            base=vr.url
        else: base=r.url
        segments=[urljoin(base,x) for x in rows if x and not x.startswith("#")][:2]
        if not segments: return Check(e,False,0,lat,"sem segmentos")
        valid=0
        for u in segments:
            try:
                start=time.monotonic()
                sr=requests.get(u,headers={"User-Agent":UA,**e.headers,"Range":"bytes=0-1023"},timeout=timeout,stream=True)
                chunk=next(sr.iter_content(1024),b""); sr.close()
                lat+=int((time.monotonic()-start)*1000)
                if sr.status_code in (200,206) and len(chunk)>=128: valid+=1
            except requests.RequestException: pass
        return Check(e,valid>0,q if valid else 0,lat,f"{valid} segmento(s) acessível(is)")
    except requests.RequestException as ex:
        return Check(e,False,0,0,type(ex).__name__)
    except Exception as ex:
        return Check(e,False,0,0,type(ex).__name__)

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",default="playlist-edited (1).m3u")
    ap.add_argument("--output",default="playlist-otimizada.m3u")
    ap.add_argument("--timeout",type=int,default=TIMEOUT)
    a=ap.parse_args()
    src=Path(a.input)
    if not src.is_file(): print(f"Arquivo original não encontrado: {src}",file=sys.stderr); return 2
    header,entries=parse(src.read_text(encoding="utf-8-sig",errors="replace"))
    if not entries: print("Nenhuma entrada M3U válida.",file=sys.stderr); return 2
    print(f"Testando {len(entries)} fontes / {len({e.key for e in entries})} canais.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results=list(pool.map(lambda e:check(e,a.timeout),entries))
    good={}
    for c in results:
        if c.ok: good.setdefault(c.entry.key,[]).append(c)
    winners={}
    for key,items in good.items():
        winners[key]=max(items,key=lambda x:(x.quality,-x.latency,-x.entry.order)).entry
    chosen=set(id(e) for e in winners.values())
    out=[header]
    for e in entries:
        if id(e) in chosen: out.extend(e.block)
    Path(a.output).write_text("\n".join(out)+"\n",encoding="utf-8")
    print(f"Aprovadas: {len(winners)} de {len({e.key for e in entries})} canais; fontes válidas: {sum(x.ok for x in results)}/{len(results)}.")
    print(f"Gerada: {a.output}")
    return 0
if __name__=="__main__": raise SystemExit(main())
