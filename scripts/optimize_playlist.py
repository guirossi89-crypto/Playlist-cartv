#!/usr/bin/env python3
from __future__ import annotations
import json, multiprocessing, math, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urljoin
import requests
from urllib3.util import Timeout as Urllib3Timeout

TIMEOUT = 12
WORKERS = 16
SOURCE_BUDGET = 12
SEGMENTS_TO_TEST = 3
FAILURES_BEFORE_REMOVAL = 2
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
    pixels: int = 0
    codec: str = ""
    fps: float = 0.0
    probe_attempted: bool = False
    probe_ok: bool = False

def quality_of(s):
    dims = re.findall(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", s)
    if dims: return max(int(w)*int(h) for w,h in dims)
    t=s.upper()
    for token,score in (("4K",3840*2160),("UHD",3840*2160),("FHD",1920*1080),("1080",1920*1080),("HD",1280*720),("720",1280*720),("SD",854*480),("540",854*480),("480",854*480),("360",640*360)):
        if token in t: return score
    return 0

def normalize_channel_key(value):
    # Remove somente sufixos de qualidade/fonte; preserva diferenças como Globo RJ e Globo SP.
    value=unescape(value or "").strip()
    value=re.sub(
        r"\s*(?:\[[^\]]*(?:4K|UHD|FHD|HD|SD|Fonte\s*\d+|\d{3,4}\s*[xX]\s*\d{3,4})[^\]]*\]|"
        r"\(\s*(?:4K|UHD|FHD|HD|SD|\d{3,4}p|\d{3,4}\s*[xX]\s*\d{3,4})\s*\))\s*$",
        "", value, flags=re.I
    )
    return re.sub(r"\s+"," ",value).strip().casefold()

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
                # Nome exibido é a identidade lógica mais estável. O tvg-id pode variar
                # entre fontes do mesmo canal ou existir apenas em parte delas.
                identity=attrs.get("tvg-name") or title or attrs.get("tvg-id") or "Canal"
                key=normalize_channel_key(identity) or normalize_channel_key(title) or normalize_channel_key(attrs.get("tvg-id")) or "canal"
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

def request_timeout(deadline, timeout):
    remaining=deadline-time.monotonic()
    if remaining <= 0: raise requests.Timeout("limite total da fonte excedido")
    cap=min(float(timeout),remaining)
    return Urllib3Timeout(total=cap,connect=min(5.0,cap),read=min(5.0,cap))

def get(url,headers,deadline,timeout):
    start=time.monotonic()
    r=requests.get(url,headers={"User-Agent":UA,**headers},timeout=request_timeout(deadline,timeout),allow_redirects=True)
    ms=int((time.monotonic()-start)*1000)
    r.raise_for_status()
    return r,ms

def is_hls_playlist(response):
    body=response.content[:1_000_000].decode(response.encoding or "utf-8",errors="replace")
    if not body.lstrip().startswith("#EXTM3U"): return None
    # Reject HTML/error pages and malformed playlists rather than approving by response size.
    head=body[:4096].lower()
    if "<html" in head or "<!doctype html" in head or "<body" in head: return None
    rows=[x.strip() for x in body.splitlines() if x.strip()]
    if not rows or rows[0] != "#EXTM3U": return None
    if any(x.startswith("#EXT-X-STREAM-INF:") for x in rows): return ("master",rows)
    has_segments=any(x and not x.startswith("#") for x in rows)
    has_media_tag=any(x.startswith(("#EXTINF:","#EXT-X-TARGETDURATION:","#EXT-X-PART:")) for x in rows)
    if has_segments and has_media_tag: return ("media",rows)
    return None

def variant_score(pixels, bandwidth, latency):
    # Combined score: resolution, declared bitrate and measured response latency.
    # Bandwidth is HLS metadata, not a direct measurement of decoded video quality.
    p=max(0,pixels or 0)
    b=max(0,bandwidth or 0)
    resolution_factor=(max(p,640*360)/(1920*1080))**0.30
    bitrate_factor=(max(b,1_500_000)/5_000_000)**0.10 if b else 1.0
    # Latência observada pesa mais que resolução/bitrate declarados.
    latency_factor=(1.0+max(0,latency)/1000.0)**0.65
    return int(1_000_000*resolution_factor*bitrate_factor/latency_factor)

def test_media_playlist(rows,base_url,headers,deadline,timeout):
    urls=[]
    for row in rows:
        if row and not row.startswith("#"):
            urls.append(urljoin(base_url,row))
            if len(urls)>=SEGMENTS_TO_TEST: break
    if not urls: return False,0,"playlist sem segmentos"
    tested=0; total_latency=0
    for url in urls:
        try:
            start=time.monotonic()
            response=requests.get(url,headers={"User-Agent":UA,**headers,"Range":"bytes=0-511"},
                timeout=request_timeout(deadline,timeout),stream=True,allow_redirects=True)
            chunk=next(response.iter_content(512),b"")
            status=response.status_code
            response.close()
            total_latency+=int((time.monotonic()-start)*1000)
            if status in (200,206) and len(chunk)>=64:
                tested+=1
            else:
                return False,total_latency,"segmento inválido"
        except requests.RequestException:
            return False,total_latency,"falha no segmento"
    return tested==len(urls),total_latency,f"{tested} segmento(s) HLS verificado(s)"

def test_transport_stream(e,deadline,timeout):
    """Valida um fluxo MPEG-TS lendo apenas o início, sem baixar o stream inteiro."""
    start=time.monotonic()
    headers={"User-Agent":UA,**e.headers,"Range":"bytes=0-1023"}
    response=requests.get(
        e.url,headers=headers,timeout=request_timeout(deadline,timeout),
        stream=True,allow_redirects=True
    )
    try:
        if response.status_code not in (200,206):
            return False,int((time.monotonic()-start)*1000),"HTTP "+str(response.status_code)
        content_type=response.headers.get("Content-Type","").lower()
        data=bytearray()
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                data.extend(chunk)
            if len(data)>=1024:
                break
        latency=int((time.monotonic()-start)*1000)
        # MPEG-TS usa pacotes de 188 bytes; também aceita variantes M2TS (192)
        # e pacotes de 204 bytes. O byte de sincronismo é 0x47.
        valid=False
        for packet_size,offset in ((188,0),(192,4),(204,0)):
            if len(data)>=offset+packet_size*4 and all(data[offset+i*packet_size]==0x47 for i in range(4)):
                valid=True
                break
        if valid:
            return True,latency,"fluxo MPEG-TS válido ("+content_type.split(";")[0]+")"
        if not data:
            return False,latency,"resposta TS vazia"
        return False,latency,"resposta não contém pacotes MPEG-TS válidos"
    finally:
        response.close()

def check(e,timeout):
    deadline=time.monotonic()+SOURCE_BUDGET
    try:
        # Endpoints .ts são fluxos contínuos, não manifestos HLS.
        # Detecta pela extensão e valida o cabeçalho binário MPEG-TS.
        if re.search(r"\.ts(?:$|[?#])",e.url,re.I):
            ok,latency,reason=test_transport_stream(e,deadline,timeout)
            score=variant_score(e.quality,0,latency)
            return Check(e,ok,score if ok else 0,latency,reason,e.quality)
        response,latency=get(e.url,e.headers,deadline,timeout)
        parsed=is_hls_playlist(response)
        if not parsed: return Check(e,False,0,latency,"resposta não é uma playlist HLS válida")
        kind,rows=parsed
        if kind=="media":
            ok,seg_latency,reason=test_media_playlist(rows,response.url,e.headers,deadline,timeout)
            score=variant_score(e.quality,0,latency+seg_latency)
            return Check(e,ok,score if ok else 0,latency+seg_latency,reason,e.quality)
        variants=[]
        for i,row in enumerate(rows):
            if row.startswith("#EXT-X-STREAM-INF:"):
                attrs=row.split(":",1)[1]
                res=re.search(r"RESOLUTION=(\d+)x(\d+)",attrs,re.I)
                pixels=int(res.group(1))*int(res.group(2)) if res else 0
                bw=re.search(r"(?:AVERAGE-BANDWIDTH|BANDWIDTH)=(\d+)",attrs,re.I)
                bandwidth=int(bw.group(1)) if bw else 0
                nxt=next((x for x in rows[i+1:] if not x.startswith("#")),None)
                if nxt:
                    # A primeira URL não-comentário após STREAM-INF é a variante correspondente.
                    variants.append((variant_score(pixels,bandwidth,latency),pixels,bandwidth,urljoin(response.url,nxt)))
        if not variants: return Check(e,False,0,latency,"manifesto master sem variantes")
        variants.sort(reverse=True)
        failures=[]
        measured=[]
        # Compara até três variantes reproduzíveis, em vez de aceitar automaticamente
        # a primeira (normalmente a de maior resolução).
        for initial_score,pixels,bandwidth,url in variants[:3]:
            try:
                variant_start=time.monotonic()
                vr,variant_latency=get(url,e.headers,deadline,timeout)
                child=is_hls_playlist(vr)
                if not child or child[0]!="media":
                    failures.append("variante não contém playlist HLS de mídia")
                    continue
                ok,seg_latency,reason=test_media_playlist(child[1],vr.url,e.headers,deadline,timeout)
                elapsed=int((time.monotonic()-variant_start)*1000)
                latency+=variant_latency+seg_latency
                if ok:
                    measured.append((variant_score(pixels,bandwidth,elapsed),elapsed,reason,pixels))
                else:
                    failures.append(reason)
            except requests.RequestException as ex:
                failures.append(type(ex).__name__)
                continue
        if measured:
            measured.sort(key=lambda x:(x[0],-x[1]),reverse=True)
            score,elapsed,reason,pixels=measured[0]
            return Check(e,True,score,elapsed,"melhor variante entre as testadas; "+reason,pixels)
        return Check(e,False,0,latency,"nenhuma variante reproduzível: "+(failures[-1] if failures else "falha"))
    except requests.RequestException as ex:
        return Check(e,False,0,0,type(ex).__name__)
    except Exception as ex:
        return Check(e,False,0,0,type(ex).__name__)

def _check_in_child(index,entry,timeout,result_queue):
    try: result_queue.put((index,check(entry,timeout)))
    except BaseException as ex: result_queue.put((index,Check(entry,False,0,0,type(ex).__name__)))

def run_checks(entries,timeout):
    ctx=multiprocessing.get_context("fork")
    result_queue=ctx.Queue(); pending=iter(enumerate(entries)); active={}
    results=[None]*len(entries); completed=0; last_reported=0; started=time.monotonic()
    def launch():
        try: index,entry=next(pending)
        except StopIteration: return False
        proc=ctx.Process(target=_check_in_child,args=(index,entry,timeout,result_queue),daemon=True)
        proc.start(); active[index]=(proc,entry,time.monotonic()+SOURCE_BUDGET); return True
    try:
        for _ in range(min(WORKERS,len(entries))): launch()
        while active:
            try:
                index,result=result_queue.get(timeout=0.10)
                item=active.pop(index,None)
                if item is not None:
                    proc,entry,deadline=item; proc.join(timeout=0.2)
                    if proc.is_alive(): proc.terminate(); proc.join(timeout=1)
                    results[index]=result; completed+=1
            except Exception: pass
            now=time.monotonic()
            for index in [i for i,(_,_,deadline) in active.items() if now>=deadline]:
                proc,entry,deadline=active.pop(index)
                if proc.is_alive(): proc.terminate()
                proc.join(timeout=1); results[index]=Check(entry,False,0,SOURCE_BUDGET*1000,"HardTimeout"); completed+=1
            for index,(proc,entry,deadline) in list(active.items()):
                if not proc.is_alive() and proc.exitcode is not None:
                    proc.join(); active.pop(index); results[index]=Check(entry,False,0,0,"ProcessExit"); completed+=1
            while len(active)<WORKERS and launch(): pass
            if completed and (completed-last_reported>=25 or completed==len(entries)):
                print(f"Progresso: {completed}/{len(entries)} fontes — {time.monotonic()-started:.1f}s",flush=True); last_reported=completed
    finally:
        for proc,entry,deadline in active.values():
            if proc.is_alive(): proc.terminate()
            proc.join(timeout=1)
        result_queue.close(); result_queue.join_thread()
    return results


def probe_stream(entry):
    """Obtém características do vídeo real sem baixar o fluxo completo."""
    headers={"User-Agent":UA,**entry.headers}
    header_blob="".join(f"{k}: {v}\\r\\n" for k,v in headers.items())
    cmd=["ffprobe","-v","error","-rw_timeout","5000000",
         "-analyzeduration","1500000","-probesize","1500000"]
    if header_blob:
        cmd.extend(["-headers",header_blob])
    cmd.extend(["-select_streams","v:0","-show_entries",
                "stream=codec_name,width,height,avg_frame_rate,bit_rate",
                "-of","json",entry.url])
    completed=subprocess.run(cmd,capture_output=True,text=True,timeout=7,check=True)
    payload=json.loads(completed.stdout or "{}")
    streams=payload.get("streams",[])
    if not streams:
        raise ValueError("FFprobe não encontrou stream de vídeo")
    stream=streams[0]
    width=int(stream.get("width") or 0)
    height=int(stream.get("height") or 0)
    if width<=0 or height<=0:
        raise ValueError("resolução efetiva indisponível")
    fps=0.0
    rate=stream.get("avg_frame_rate","0/0")
    if "/" in rate:
        numerator,denominator=rate.split("/",1)
        if float(denominator or 0)>0:
            fps=float(numerator)/float(denominator)
    bitrate=int(stream.get("bit_rate") or 0)
    return {"pixels":width*height,"codec":str(stream.get("codec_name") or ""),
            "fps":fps,"bitrate":bitrate,"width":width,"height":height}

def probe_best_candidates(good, history, ranking):
    """Sonda somente as duas melhores fontes aprovadas de cada canal."""
    if not shutil.which("ffprobe"):
        print("FFprobe indisponível; mantendo avaliação pelo manifesto.",flush=True)
        return
    targets={}
    for items in good.values():
        ordered=sorted(items,key=lambda x:(ranking(x,items),x.quality,-x.latency),reverse=True)
        for result in ordered[:2]:
            targets.setdefault(result.entry.url,result)
    if not targets:
        return
    print(f"FFprobe: avaliando {len(targets)} candidatas (até 2 por canal), com 8 verificações simultâneas.",flush=True)
    completed=0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures={pool.submit(probe_stream,result.entry):result for result in targets.values()}
        for future in as_completed(futures):
            result=futures[future]
            result.probe_attempted=True
            try:
                measured=future.result()
                result.pixels=measured["pixels"]
                result.codec=measured["codec"]
                result.fps=measured["fps"]
                result.probe_ok=True
                # Usa resolução real e bitrate real quando o container os informa.
                # A latência fica exclusivamente no componente de latência do ranking.
                result.quality=variant_score(result.pixels,measured["bitrate"],0)
                result.reason += f"; FFprobe {measured['width']}x{measured['height']} {result.codec}"
            except Exception as ex:
                result.reason += "; FFprobe inconclusivo: "+type(ex).__name__
            completed+=1
            if completed%25==0 or completed==len(futures):
                print(f"FFprobe: {completed}/{len(futures)} candidatas avaliadas.",flush=True)

def load_state(path):
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError): return {}

def main():
    import argparse
    from datetime import datetime, timezone
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",default="playlist-edited (1).m3u")
    ap.add_argument("--extra-input",action="append",default=[],help="M3U complementar; pode ser informado mais de uma vez")
    ap.add_argument("--output",default="playlist-otimizada.m3u")
    ap.add_argument("--state",default="playlist-health-state.json")
    ap.add_argument("--report",default="playlist-comparison.md")
    ap.add_argument("--timeout",type=int,default=TIMEOUT)
    a=ap.parse_args()
    src=Path(a.input); output=Path(a.output); state_path=Path(a.state)
    if not src.is_file(): print(f"Arquivo original não encontrado: {src}",file=sys.stderr); return 2
    header,entries=parse(src.read_text(encoding="utf-8-sig",errors="replace"))
    for extra_name in a.extra_input:
        extra=Path(extra_name)
        if not extra.is_file():
            print(f"Arquivo complementar não encontrado: {extra}",file=sys.stderr); return 2
        _,extra_entries=parse(extra.read_text(encoding="utf-8-sig",errors="replace"))
        for e in extra_entries:
            e.order=len(entries); entries.append(e)
    if not entries: print("Nenhuma entrada M3U válida.",file=sys.stderr); return 2

    _,previous_entries=parse(output.read_text(encoding="utf-8-sig",errors="replace")) if output.is_file() else ("#EXTM3U",[])
    previous={}
    for entry in previous_entries:
        entry.order=-1
        previous.setdefault(entry.key,entry)

    state=load_state(state_path)
    failures=state.get("failures",{}) if isinstance(state.get("failures",{}),dict) else {}
    history=state.get("sources",{}) if isinstance(state.get("sources",{}),dict) else {}
    old_scores={url:float(item.get("last_rank_score",0)) for url,item in history.items() if isinstance(item,dict)}
    now=datetime.now(timezone.utc)
    now_iso=now.isoformat()
    now_ts=now.timestamp()

    # Cache adaptativo: somente fontes com histórico consistente podem pular uma rodada.
    # O cache não registra uma nova aprovação; apenas reutiliza a última evidência válida.
    cached={}
    pending=[]
    for e in entries:
        h=history.get(e.url,{})
        if isinstance(h,dict):
            total=int(h.get("successes",0))+int(h.get("failures",0))
            reliability=(int(h.get("successes",0))+2)/(total+2) if total else 0
            age=now_ts-float(h.get("last_checked_ts",0) or 0)
            cache_ttl=90*60 if int(h.get("successes",0))>=4 and reliability>=0.90 else 0
            if cache_ttl and age>=0 and age<cache_ttl and h.get("last_ok") and int(h.get("last_score",0))>0:
                cached[e.order]=Check(e,True,int(h["last_score"]),int(h.get("last_latency",0)),"cache de fonte historicamente estável",int(h.get("last_effective_pixels",0)),str(h.get("last_codec", "")),float(h.get("last_fps",0)),False,bool(h.get("last_probe_ok",False)))
                continue
        pending.append(e)

    channels=len({e.key for e in entries})
    print(f"Testando {len(pending)} fontes / {channels} canais; {len(cached)} fontes reaproveitadas do cache; {WORKERS} verificações simultâneas.",flush=True)
    results=run_checks(pending,a.timeout) if pending else []
    by_order={r.entry.order:r for r in results}
    by_order.update(cached)
    results=[by_order[e.order] for e in entries if e.order in by_order]

    # Atualiza histórico por URL. Falhas isoladas reduzem a confiabilidade, mas não apagam a fonte.
    for result in results:
        if result.reason.startswith("cache de "): continue
        h=history.setdefault(result.entry.url,{"successes":0,"failures":0})
        h["successes"]=int(h.get("successes",0))+(1 if result.ok else 0)
        h["failures"]=int(h.get("failures",0))+(0 if result.ok else 1)
        h["last_checked"]=now_iso
        h["last_checked_ts"]=now_ts
        h["last_ok"]=bool(result.ok)
        h["last_latency"]=int(result.latency)
        h["last_score"]=int(result.quality if result.ok else 0)
        h["last_reason"]=result.reason[:180]
        if result.probe_attempted:
            h["last_probe_ok"]=bool(result.probe_ok)
            if result.probe_ok:
                h["last_effective_pixels"]=int(result.pixels)
                h["last_codec"]=result.codec
                h["last_fps"]=round(result.fps,3)

    good={}
    for result in results:
        if result.ok: good.setdefault(result.entry.key,[]).append(result)

    # Score moderado: qualidade 50%, confiabilidade histórica 35%, latência 15%.
    # A confiabilidade usa suavização para não penalizar excessivamente fontes novas.
    def ranking(result, candidates):
        h=history.get(result.entry.url,{})
        successes=int(h.get("successes",0)); failed=int(h.get("failures",0))
        reliability=(successes+2)/(successes+failed+2)
        max_quality=max((x.quality for x in candidates),default=1) or 1
        quality_component=max(0,min(1,result.quality/max_quality))
        latency_component=1/(1+max(0,result.latency)/2000)
        score=0.50*quality_component+0.35*reliability+0.15*latency_component
        return score

    # Reavalia por FFprobe apenas as duas melhores candidatas já aprovadas de cada canal.
    # Uma falha do FFprobe não reprova a fonte: mantém-se o score do manifesto.
    probe_best_candidates(good,history,ranking)

    winners={}; winner_scores={}
    for key,items in good.items():
        selected=max(items,key=lambda x:(ranking(x,items),x.quality,-x.latency,-x.entry.order))
        winners[key]=selected.entry
        winner_scores[key]=ranking(selected,items)
        history.setdefault(selected.entry.url,{})["last_rank_score"]=winner_scores[key]

    new_failures={}
    retained=0
    if not winners:
        print("Nenhum canal foi validado nesta execução; saída e histórico preservados por segurança.",file=sys.stderr,flush=True)
        return 3

    for key in {e.key for e in entries}:
        if key in winners:
            continue
        count=int(failures.get(key,0))+1
        if key in previous and count<FAILURES_BEFORE_REMOVAL:
            winners[key]=previous[key]; retained+=1
            new_failures[key]=count
            print(f"Falha {count}/{FAILURES_BEFORE_REMOVAL}: mantendo temporariamente {previous[key].label}.",flush=True)
        elif count<FAILURES_BEFORE_REMOVAL:
            new_failures[key]=count

    chosen_orders={e.order for e in winners.values() if e.order>=0}
    out=[header]; emitted=set()
    for e in entries:
        if e.order in chosen_orders and e.key in winners and winners[e.key].order==e.order:
            out.extend(e.block); emitted.add(e.key)
    for key,e in winners.items():
        if key not in emitted and e.order<0:
            out.extend(e.block); emitted.add(key)
    if len(out)<=1:
        print("Erro: saída sem canais; arquivo não alterado.",file=sys.stderr); return 3

    # Relatório comparativo da seleção anterior contra a nova.
    current_scores={winners[k].url:winner_scores.get(k,0) for k in winners}
    old_map={e.key:e for e in previous_entries}
    new_map=winners
    improved=[]; worsened=[]; changed=[]; appeared=[]; disappeared=[]
    for key,e in new_map.items():
        old=old_map.get(key)
        if old is None: appeared.append((key,e.label)); continue
        if old.url!=e.url:
            changed.append((key,old.label, e.label))
        old_score=old_scores.get(old.url,0)
        new_score=current_scores.get(e.url,old_score)
        if old_score and new_score:
            if new_score>old_score+0.03: improved.append((key,old_score,new_score))
            elif new_score<old_score-0.03: worsened.append((key,old_score,new_score))
    for key,e in old_map.items():
        if key not in new_map: disappeared.append((key,e.label))

    output.write_text("\n".join(out)+"\n",encoding="utf-8")
    state_path.write_text(json.dumps({"version":2,"failures":new_failures,"sources":history},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines=["# Relatório comparativo da playlist","",f"- Execução: {now_iso}",f"- Canais publicados: {len(winners)}",f"- Fontes testadas: {len(pending)}",f"- Fontes reaproveitadas do cache: {len(cached)}",f"- Canais mantidos temporariamente: {retained}",""]
    def section(title,items,formatter):
        lines.extend([f"## {title} ({len(items)})",""])
        if not items: lines.append("- Nenhum.")
        else: lines.extend("- "+formatter(x) for x in sorted(items,key=lambda x:x[0]))
        lines.append("")
    section("Melhoraram",improved,lambda x:f"{x[0]} — score {x[1]:.3f} → {x[2]:.3f}")
    section("Pioraram",worsened,lambda x:f"{x[0]} — score {x[1]:.3f} → {x[2]:.3f}")
    section("Trocaram de fonte",changed,lambda x:f"{x[0]} — fonte anterior: {x[1]} | nova: {x[2]}")
    section("Desapareceram",disappeared,lambda x:f"{x[0]} — {x[1]}")
    section("Novos canais",appeared,lambda x:f"{x[0]} — {x[1]}")
    Path(a.report).write_text("\n".join(lines)+"\n",encoding="utf-8")
    valid=sum(x.ok for x in results)
    print(f"Aprovados: {len(winners)} canais ({retained} mantidos por tolerância); fontes válidas/reutilizadas: {valid}/{len(results)}.",flush=True)
    print(f"Relatório comparativo gerado: {a.report}",flush=True)
    print(f"Gerada: {a.output} ({len(winners)} canais)",flush=True)
    return 0

if __name__=="__main__": raise SystemExit(main())
