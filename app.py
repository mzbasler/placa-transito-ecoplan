# -*- coding: utf-8 -*-
"""
Painel web local (v2) para testar e operar o detector de placas.
Roda em 127.0.0.1 (so nesta maquina). Inicie com:  python app.py
(ou dois cliques em INICIAR_PAINEL.bat)

Arquitetura: a DETECCAO (lenta, CPU) roda uma vez e salva deteccoes.csv + recortes
locais. Tudo o mais (limiar, filtros, avaliacao, mosaico, sequencia, mapa, exportar)
recalcula na hora a partir desse cache -- por isso o slider responde ao vivo.
"""
import os, re, csv, json, time, glob, threading, subprocess, webbrowser, urllib.parse, shutil
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.abspath(__file__))
SAIDAS = os.path.join(RAIZ, "saidas")
DADOS = os.path.join(RAIZ, "dados")
OUTPUT = os.path.join(RAIZ, "output")   # fotos de referencia (quadro inteiro c/ a caixa)
ROOT_IMAGENS = r"\\192.168.0.210\Setores\Setor Dev\_TESTES\Placas"
PORTA = int(os.environ.get("PLACAS_PORT", "8765"))
CONF_SOLO = 0.70   # deteccao com conf >= isso vira placa mesmo em poucos quadros (sinal claro)
# limiares do rastreamento de trajetoria (1 placa fisica = 1 trajetoria de caixas);
# usados tanto pelo TrackerVivo (ao vivo) quanto por deduplicar (lote/calibracao).
MAXGAP = 0.03      # km: vao maximo sem deteccao antes de fechar a placa
AREA_KEEP = 0.6    # a mesma placa nao encolhe abaixo disso entre quadros
CX_BACK = 90.0     # px: recuo horizontal tolerado (jitter) antes de virar placa nova
XSTEP = 700.0      # px: passo horizontal maximo entre quadros da mesma placa

ESTADO = {
    "running": False, "done": False, "error": None, "step": "ocioso",
    "processed": 0, "total": 0, "found": 0, "log": [], "out": None,
    "t0": 0.0, "eta_s": 0, "elapsed_s": 0, "folders": [], "placas": [],
}
LOCK = threading.Lock()
PARAR = {"flag": False, "proc": None}   # controle do botao Parar
_folders_cache = None


def log(msg):
    with LOCK:
        ESTADO["log"].append(msg)
        ESTADO["log"] = ESTADO["log"][-80:]


# ------------------------------------------------------------------ pastas
def listar_pastas(root, max_dirs=800, max_result=120):
    global _folders_cache
    if _folders_cache is not None:
        return _folders_cache
    achadas, n = [], 0
    try:
        for dp, dn, fn in os.walk(root):
            n += 1
            if n > max_dirs:
                break
            if any(f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")) for f in fn):
                achadas.append(dp)
            if len(achadas) >= max_result:
                break
    except Exception as e:
        log(f"[aviso] listar pastas: {e}")
    _folders_cache = sorted(achadas)
    return _folders_cache


def listar_gabaritos():
    return sorted(glob.glob(os.path.join(DADOS, "*.csv")))


# ------------------------------------------------------------------ deteccao
def worker(folders, imgsz, limite, preset):
    try:
        nome = os.path.basename(folders[0].rstrip("\\/")) or "pasta"
        out = os.path.join(SAIDAS, "PAINEL_" + re.sub(r"[^\w\-]", "_", nome))
        with LOCK:
            ESTADO.update({"out": out, "step": "detectando", "processed": 0,
                           "total": 0, "found": 0, "t0": time.time(),
                           "folders": folders, "placas": [], "preview": None})
        args = ["python", os.path.join(RAIZ, "src", "detectar.py"),
                "--pastas", ";".join(folders), "--stride", "1",
                "--conf", "0.10", "--imgsz", str(imgsz), "--out", out]
        if limite > 0:
            args += ["--max", str(limite)]
        env = dict(os.environ, PYTHONUNBUFFERED="1", YOLO_VERBOSE="False",
                   PYTHONIOENCODING="utf-8")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", bufsize=1, env=env, cwd=RAIZ)
        PARAR["proc"] = p
        # agrupamento AO VIVO: cada deteccao do detector entra no rastreador; quando a
        # placa "fecha" (veiculo passou), ela cai na tabela na hora.
        trk = TrackerVivo(int(preset.get("min_quadros", 3)))
        cf = float(preset.get("conf", 0.35)); ma = float(preset.get("min_area", 0.0002))
        mnasp = float(preset.get("min_aspecto", 0.45)); mxasp = float(preset.get("max_aspecto", 4.0))
        ladof = preset.get("lado", "ambos")

        def empurra(novas):
            if not novas:
                return
            with LOCK:
                ESTADO["placas"].extend(novas)
                ESTADO["placas"].sort(key=lambda x: x["km"])
                ESTADO["found"] = len(ESTADO["placas"])
                ESTADO["preview"] = novas[-1].get("crop")

        ultima = ""
        for linha in p.stdout:
            linha = linha.rstrip("\n")
            if not linha:
                continue
            if linha.startswith("@DET\t"):
                f = linha.split("\t")
                if len(f) >= 12:
                    try:
                        km = float(f[1]); x1 = int(f[2]); y1 = int(f[3]); x2 = int(f[4]); y2 = int(f[5])
                        conf = float(f[6]); area = float(f[7]); comp = int(f[8]); lado = f[9]
                        if conf < cf or area < ma or (ladof in ("E", "D") and lado != ladof):
                            continue
                        asp = (x2 - x1) / max(1.0, (y2 - y1))
                        if not (mnasp <= asp <= mxasp):
                            continue
                        d = {"km": km, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": conf,
                             "area_frac": area, "completa": comp, "lado_img": lado,
                             "aspecto": asp, "imagem": f[10], "crop": f[11]}
                        empurra(trk.add(d))
                    except Exception:
                        pass
                continue
            linha = linha.strip()
            ultima = linha
            m = re.search(r"(\d+) imagens a processar", linha)
            if m:
                with LOCK:
                    ESTADO["total"] = int(m.group(1))
            m = re.search(r"\[prog\] (\d+)/(\d+)", linha)
            if m:
                proc, tot = int(m.group(1)), int(m.group(2))
                el = time.time() - ESTADO["t0"]
                eta = (el / proc) * (tot - proc) if proc else 0
                with LOCK:
                    ESTADO.update({"processed": proc, "total": tot,
                                   "elapsed_s": int(el), "eta_s": int(eta)})
                    nph = ESTADO["found"]
                # console fidedigno: placas UNICAS ja fechadas (= "Detectadas"), nao a
                # contagem crua de deteccoes (uma placa rende dezenas de deteccoes).
                log(f"[prog] {proc}/{tot} quadros · {nph} placas")
                continue
            if linha.startswith("[info]"):
                log(linha)
        empurra(trk.finalizar())   # fecha as placas que sobraram
        p.wait()
        if PARAR["flag"]:
            with LOCK:
                ESTADO.update({"step": "interrompido", "running": False, "done": False,
                               "eta_s": 0})
            log("[parado] Processo interrompido pelo usuario.")
            return
        if p.returncode != 0:
            msg = ultima.replace("[erro]", "").strip() or "deteccao terminou com erro"
            if "nenhuma imagem" in msg.lower():
                msg = "Nenhuma imagem encontrada nessa pasta (formatos aceitos: jpg, jpeg, png, bmp, tif, webp)."
            raise RuntimeError(msg)
        with LOCK:
            ESTADO.update({"step": "deteccao pronta", "done": True, "running": False,
                           "processed": ESTADO["total"] or ESTADO["processed"],
                           "elapsed_s": int(time.time() - ESTADO["t0"]), "eta_s": 0})
        log("[ok] Deteccao concluida. Ajuste os filtros e veja o resultado ao vivo.")
    except Exception as e:
        with LOCK:
            ESTADO.update({"error": str(e), "running": False, "step": "erro"})
        log(f"[ERRO] {e}")


# ------------------------------------------------------------------ conferencia AO VIVO (stream)
def _montar_placa(g):
    """Monta o registro de uma placa a partir do grupo de deteccoes: escolhe o melhor
    quadro (mais proximo + inteiro) e anexa a sequencia de membros em ordem de km."""
    melhor = sorted(g, key=lambda x: (x["completa"], x["area_frac"]))[-1]
    membros = [{"km": round(d["km"], 3), "conf": round(d["conf"], 3),
                "area": round(d["area_frac"], 5), "completa": d["completa"],
                "crop": d.get("crop", "")} for d in sorted(g, key=lambda x: x["km"])]
    return {"km": round(melhor["km"], 3), "lado": melhor["lado_img"],
            "conf": round(melhor["conf"], 4), "area_frac": round(melhor["area_frac"], 6),
            "completa": melhor["completa"], "n_quadros": len(g),
            "x1": melhor["x1"], "y1": melhor["y1"], "x2": melhor["x2"], "y2": melhor["y2"],
            "imagem": melhor["imagem"], "crop": melhor.get("crop", ""), "membros": membros}


class TrackerVivo:
    """Agrupa deteccoes em tempo real: cada placa fisica e' uma trajetoria; a placa
    'fecha' quando o veiculo passa dela (gap de km), e so' ai entra na lista.
    A MESMA placa so' CRESCE (se aproxima) e ANDA p/ a borda (direita no lado D,
    esquerda no lado E); detecao que encolheu ou voltou p/ o centro e' OUTRA placa
    -- e' assim que setas de curva coladas deixam de ser fundidas numa so'.
    Os limiares (MAXGAP/AREA_KEEP/CX_BACK/XSTEP) sao constantes de modulo."""

    def __init__(self, min_quadros):
        self.min_quadros = min_quadros
        self.tracks = {"E": [], "D": []}

    def _finaliza(self, t):
        g = t["dets"]
        # vira placa se teve quadros suficientes OU se houve deteccao de ALTA confianca
        if len(g) < self.min_quadros and max(d["conf"] for d in g) < CONF_SOLO:
            return None
        return _montar_placa(g)

    def add(self, d):
        """processa 1 deteccao; devolve lista de placas que FECHARAM agora."""
        lado = d["lado_img"]
        if lado not in self.tracks:
            return []
        d["cx"] = (float(d["x1"]) + float(d["x2"])) / 2.0
        saida = 1.0 if lado == "D" else -1.0   # sentido em que a placa anda ao se aproximar
        prontas, abertos = [], []
        for t in self.tracks[lado]:
            if d["km"] - t["last_km"] > MAXGAP:   # passou da placa -> fecha
                pl = self._finaliza(t)
                if pl:
                    prontas.append(pl)
            else:
                abertos.append(t)
        self.tracks[lado] = abertos
        melhor_t, melhor_dx = None, 1e9
        for t in self.tracks[lado]:
            # a MESMA placa so cresce; se encolheu muito, e' outra placa (mais ao fundo)
            if d["area_frac"] < t["last_area"] * AREA_KEEP:
                continue
            # ... e so anda p/ a borda; se "voltou" p/ o centro, e' uma placa NOVA
            if (d["cx"] - t["last_cx"]) * saida < -CX_BACK:
                continue
            dx = abs(d["cx"] - t["last_cx"])
            if dx <= XSTEP and dx < melhor_dx:
                melhor_dx, melhor_t = dx, t
        if melhor_t is not None:
            melhor_t["dets"].append(d); melhor_t["last_km"] = d["km"]
            melhor_t["last_cx"] = d["cx"]; melhor_t["last_area"] = d["area_frac"]
        else:
            self.tracks[lado].append({"dets": [d], "last_km": d["km"], "last_cx": d["cx"],
                                      "last_area": d["area_frac"]})
        return prontas

    def finalizar(self):
        out = []
        for lado in ("E", "D"):
            for t in self.tracks[lado]:
                pl = self._finaliza(t)
                if pl:
                    out.append(pl)
            self.tracks[lado] = []
        return out


def replay_worker(out, conf, min_quadros, min_asp, max_asp, min_area, lado_filtro, pace):
    """Reproduz o cache (deteccoes.csv) em ordem de km, populando ESTADO['placas'] ao vivo."""
    try:
        dets = ler_deteccoes(out)
        # deriva a pasta original do proprio cache (coluna 'imagem') p/ o mapa achar o GPX
        folders = [os.path.dirname(dets[0]["imagem"])] if dets and dets[0].get("imagem") else []
        sel = [d for d in dets if d["conf"] >= conf and min_asp <= d["aspecto"] <= max_asp
               and d["area_frac"] >= min_area
               and (lado_filtro not in ("E", "D") or d["lado_img"] == lado_filtro)]
        sel.sort(key=lambda d: d["km"])
        total = len(sel)
        with LOCK:
            ESTADO.update({"running": True, "done": False, "error": None, "step": "conferindo…",
                           "processed": 0, "total": total, "found": 0, "placas": [],
                           "out": out, "folders": folders, "t0": time.time(), "preview": None})
        trk = TrackerVivo(min_quadros)
        for i, d in enumerate(sel):
            if PARAR["flag"]:
                break
            prontas = trk.add(d)
            if prontas:
                with LOCK:
                    ESTADO["placas"].extend(prontas)
                    ESTADO["placas"].sort(key=lambda p: p["km"])
                    ESTADO["found"] = len(ESTADO["placas"])
                    ESTADO["preview"] = prontas[-1].get("crop")
            if i % 8 == 0:
                with LOCK:
                    ESTADO["processed"] = i
                    ESTADO["step"] = f"conferindo… km {d['km']:.2f}"
            if pace > 0:
                time.sleep(pace)
        finais = trk.finalizar()
        with LOCK:
            ESTADO["placas"].extend(finais)
            ESTADO["placas"].sort(key=lambda p: p["km"])
            ESTADO["found"] = len(ESTADO["placas"])
            ESTADO["processed"] = total
            ESTADO.update({"running": False, "done": True, "step": "concluído ✓"})
        salvar_placas(out, ESTADO["placas"])
    except Exception as e:
        with LOCK:
            ESTADO.update({"running": False, "error": str(e), "step": "erro"})


# ------------------------------------------------------------------ dedup (em memoria, instantaneo)
def ler_deteccoes(out):
    fp = os.path.join(out, "deteccoes.csv")
    if not os.path.exists(fp):
        return []
    with open(fp, encoding="utf-8-sig") as f:
        dets = list(csv.DictReader(f))
    for d in dets:
        d["km"] = float(d["km"]); d["conf"] = float(d["conf"])
        d["area_frac"] = float(d["area_frac"]); d["completa"] = int(d["completa"])
        h = max(1.0, float(d["y2"]) - float(d["y1"]))
        d["aspecto"] = (float(d["x2"]) - float(d["x1"])) / h
    return dets


def deduplicar(dets, conf, min_quadros, min_asp, max_asp, min_area, lado_filtro):
    """Agrupa deteccoes da MESMA placa fisica e mantem o melhor quadro de cada uma.
    Reusa o MESMO motor do TrackerVivo (ao vivo): filtra as deteccoes, ordena por km
    e alimenta o tracker em sequencia -- assim a calibracao (calibrar.py) roda exatamente
    a logica de producao, sem uma segunda implementacao para divergir."""
    sel = [d for d in dets if d["conf"] >= conf and min_asp <= d["aspecto"] <= max_asp
           and d["area_frac"] >= min_area
           and (lado_filtro not in ("E", "D") or d["lado_img"] == lado_filtro)]
    sel.sort(key=lambda d: d["km"])
    trk = TrackerVivo(min_quadros)
    placas = []
    for d in sel:
        placas.extend(trk.add(d))
    placas.extend(trk.finalizar())
    placas.sort(key=lambda x: (x["km"], 0 if x["lado"] == "E" else 1))   # km; desempata E antes de D
    return placas


# ------------------------------------------------------------------ conferencia (✓/✗) no servidor
def _conf_file(run):
    return os.path.join(SAIDAS, run, "conferencia.json")


def _run_default_nome(run):
    """Nome padrao da conferencia = pasta de onde a deteccao foi feita (coluna 'imagem')."""
    try:
        with open(os.path.join(SAIDAS, run, "deteccoes.csv"), encoding="utf-8-sig") as f:
            row = next(csv.DictReader(f), None)
        if row and row.get("imagem"):
            d = os.path.dirname(row["imagem"].rstrip("\\/"))
            base = os.path.basename(d)
            if re.match(r"^cam\s*\d+$", base, re.I):   # pasta de camera -> usa a pasta-mae
                base = os.path.basename(os.path.dirname(d))
            return base or run
    except Exception:
        pass
    return run


def carregar_conf(run):
    fp = _conf_file(run)
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            return {"nome": d.get("nome") or _run_default_nome(run),
                    "marks": d.get("marks") or {}}
        except Exception:
            pass
    return {"nome": _run_default_nome(run), "marks": {}}


def salvar_conf(run, nome, marks):
    fp = _conf_file(run)
    if not os.path.isdir(os.path.dirname(fp)):
        return False
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"nome": nome, "marks": marks}, f, ensure_ascii=False)
    os.replace(tmp, fp)   # gravacao atomica -> nunca corrompe
    return True


def salvar_placas(out, placas):
    fp = os.path.join(out, "placas_unicas.csv")
    with open(fp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["km", "lado", "conf", "area_frac", "completa",
                                          "n_quadros", "x1", "y1", "x2", "y2", "imagem", "crop"],
                           extrasaction="ignore")
        w.writeheader(); w.writerows(placas)
    return fp


def _cv2_ler(caminho):
    """cv2.imread falha silenciosamente com caminhos Unicode no Windows; usa fromfile."""
    import cv2, numpy as np
    try:
        arr = np.fromfile(caminho, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _cv2_salvar(caminho, img):
    """cv2.imwrite também falha com caminhos Unicode; usa imencode + tofile."""
    import cv2, numpy as np
    try:
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            np.array(buf).tofile(caminho)
            return True
    except Exception:
        pass
    return False


def gerar_referencia(out, placa, i):
    caminho_orig = placa["imagem"]
    img = _cv2_ler(caminho_orig)
    if img is None:
        nome = os.path.basename(caminho_orig)
        for pasta in (ESTADO.get("folders") or []):
            img = _cv2_ler(os.path.join(pasta, nome))
            if img is not None:
                break
    if img is None:
        return None
    import cv2
    x1, y1 = int(float(placa["x1"])), int(float(placa["y1"]))
    x2, y2 = int(float(placa["x2"])), int(float(placa["y2"]))
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), 5)
    rot = f"#{i+1}  km {float(placa['km']):.3f}  lado {placa['lado']}  conf {float(placa['conf']):.2f}"
    cv2.rectangle(img, (0, 0), (min(img.shape[1], 980), 48), (0, 0, 0), -1)
    cv2.putText(img, rot, (12, 33), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    run = os.path.basename(out.rstrip("\\/"))
    odir = os.path.join(OUTPUT, run)
    os.makedirs(odir, exist_ok=True)
    nome = f"placa_{i+1:03d}_km{float(placa['km']):.3f}_{placa['lado']}.jpg".replace(" ", "_")
    cam = os.path.join(odir, nome)
    _cv2_salvar(cam, img)
    return cam


# ------------------------------------------------------------------ avaliacao vs gabarito
def avaliar(placas, gabarito_path, tol_m, usar_lado):
    if not gabarito_path or not os.path.exists(gabarito_path):
        return None
    with open(gabarito_path, encoding="utf-8-sig") as f:
        gt = list(csv.DictReader(f))
    for g in gt:
        try:
            g["_km"] = float(g.get("km_abs") or g.get("km"))
        except Exception:
            g["_km"] = None
    gt = [g for g in gt if g["_km"] is not None]
    if not placas or not gt:
        return {"erro": "sem dados"}
    tol = tol_m / 1000.0
    dlo, dhi = min(p["km"] for p in placas), max(p["km"] for p in placas)
    glo, ghi = min(g["_km"] for g in gt), max(g["_km"] for g in gt)
    ov_lo, ov_hi = max(dlo, glo), min(dhi, ghi)
    if ov_hi <= ov_lo:
        return {"sem_overlap": True, "img_km": [round(dlo, 1), round(dhi, 1)],
                "gab_km": [round(glo, 1), round(ghi, 1)]}
    gtf = [g for g in gt if ov_lo - tol <= g["_km"] <= ov_hi + tol]
    usados = set(); achadas = 0; faltaram = []
    for g in gtf:
        bi, bd = None, 1e9
        for i, p in enumerate(placas):
            if i in usados:
                continue
            if usar_lado and p.get("lado") and g.get("lado") and p["lado"] != g["lado"]:
                continue
            dd = abs(p["km"] - g["_km"])
            if dd < bd:
                bd, bi = dd, i
        if bi is not None and bd <= tol:
            usados.add(bi); achadas += 1
        else:
            faltaram.append({"km": round(g["_km"], 3), "lado": g.get("lado", ""),
                             "codigo": g.get("codigo", "")})
    falsos = [i for i in range(len(placas)) if i not in usados]
    n_gt, n_det = len(gtf), len(placas)
    return {
        "n_gt": n_gt, "n_det": n_det, "achadas": achadas, "fp": len(falsos),
        "recall": round(100 * achadas / n_gt, 1) if n_gt else 0,
        "fp_rel": round(100 * len(falsos) / n_gt, 1) if n_gt else 0,
        "faltaram": faltaram[:60], "falsos_idx": falsos,
    }


# ------------------------------------------------------------------ GPX -> mapa
def parse_gpx_pasta(folders):
    pts = []
    for folder in folders:
        folder = folder.rstrip("\\/")
        # o GPX pode ter o nome da PASTA ou da PASTA-MAE (cobre .../<TRECHO>/Cam1,
        # onde o GPX se chama <TRECHO>.GPX e fica um nivel acima)
        nomes = [os.path.basename(folder).lower(),
                 os.path.basename(os.path.dirname(folder)).lower()]
        # procura subindo: a propria pasta, a mae e a avo
        dirs = [folder, os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))]
        alvo = None
        for dd in dirs:                      # 1) casa pelo nome (preferencial)
            if not dd:
                continue
            cands = glob.glob(os.path.join(dd, "*.GPX")) + glob.glob(os.path.join(dd, "*.gpx"))
            for c in cands:
                if os.path.splitext(os.path.basename(c))[0].lower() in nomes:
                    alvo = c; break
            if alvo:
                break
        if not alvo:                         # 2) fallback: um unico GPX no dir mais proximo
            for dd in dirs:
                if not dd:
                    continue
                cands = glob.glob(os.path.join(dd, "*.GPX")) + glob.glob(os.path.join(dd, "*.gpx"))
                if len(cands) == 1:
                    alvo = cands[0]; break
        if not alvo:
            continue
        try:
            tree = ET.parse(alvo)
            for el in tree.iter():
                if el.tag.endswith("trkpt") or el.tag.endswith("rtept") or el.tag.endswith("wpt"):
                    la, lo = el.get("lat"), el.get("lon")
                    if la and lo:
                        pts.append([float(la), float(lo)])
        except Exception as e:
            log(f"[aviso] gpx {alvo}: {e}")
    return pts


# ------------------------------------------------------------------ servidor
def dentro(base, caminho):
    return os.path.realpath(caminho).startswith(os.path.realpath(base))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file_img(self, path, base=None):
        base = base or SAIDAS
        if not path or not os.path.exists(path) or not dentro(base, path):
            self._send(404, "text/plain", b"sem imagem"); return
        with open(path, "rb") as f:
            self._send(200, "image/jpeg", f.read())

    def _static(self, path):
        base = os.path.join(RAIZ, "web")
        if not os.path.isfile(path) or not dentro(base, path):
            self._send(404, "text/plain", b"nao encontrado"); return
        ext = os.path.splitext(path)[1].lower()
        ct = {".css": "text/css", ".js": "application/javascript", ".png": "image/png",
              ".html": "text/html; charset=utf-8"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, ct, f.read())

    # -------------------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        out = ESTADO.get("out")
        if u.path == "/favicon.ico":
            self._send(200, "image/x-icon", b"")
        elif u.path.startswith("/vendor/"):
            self._static(os.path.join(RAIZ, "web", "vendor", os.path.basename(u.path)))
        elif u.path == "/":
            with open(os.path.join(RAIZ, "web", "index.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif u.path == "/mapa":
            with open(os.path.join(RAIZ, "web", "mapa.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif u.path == "/api/folders":
            self._json({"root": ROOT_IMAGENS, "pastas": listar_pastas(ROOT_IMAGENS),
                        "gabaritos": listar_gabaritos()})
        elif u.path == "/api/status":
            with LOCK:
                s = {k: ESTADO[k] for k in ("running", "done", "error", "step",
                     "processed", "total", "found", "eta_s", "elapsed_s")}
                s["log"] = ESTADO["log"][-20:]
            self._json(s)
        elif u.path == "/api/placas":
            with LOCK:
                placas = ESTADO.get("placas") or []
                slim = [{k: v for k, v in p.items() if k != "membros"} for p in placas]
                st = {"running": ESTADO["running"], "done": ESTADO["done"],
                      "processed": ESTADO["processed"], "total": ESTADO["total"],
                      "found": ESTADO["found"], "step": ESTADO["step"],
                      "preview": ESTADO.get("preview"),
                      "run": os.path.basename((ESTADO.get("out") or "").rstrip("\\/"))}
            self._json({"placas": slim, "status": st})
        elif u.path == "/api/runs":
            rs = []
            if os.path.isdir(SAIDAS):
                for run in os.listdir(SAIDAS):
                    fp = os.path.join(SAIDAS, run, "deteccoes.csv")
                    if os.path.isfile(fp):
                        c = carregar_conf(run)
                        rs.append({"run": run, "nome": c["nome"],
                                   "n_marks": len(c["marks"]), "mtime": os.path.getmtime(fp)})
            rs.sort(key=lambda r: -r["mtime"])
            self._json({"runs": rs})
        elif u.path == "/api/conf":
            run = q.get("run", [""])[0]
            if not run or not os.path.isdir(os.path.join(SAIDAS, run)):
                self._json({"erro": "rodada nao encontrada"}, 404); return
            self._json(carregar_conf(run))
        elif u.path == "/api/latest":
            d = os.path.join(out, "anotadas") if out else None
            arqs = ([os.path.join(d, x) for x in os.listdir(d) if x.lower().endswith(".jpg")]
                    if d and os.path.isdir(d) else [])
            self._file_img(max(arqs, key=os.path.getmtime) if arqs else None)
        elif u.path == "/api/file":
            self._file_img((q.get("path", [""])[0]))
        elif u.path == "/api/referencia":
            i = int(q.get("i", [-1])[0])
            placas = ESTADO.get("placas") or []
            if out and 0 <= i < len(placas):
                self._file_img(gerar_referencia(out, placas[i], i), base=OUTPUT)
            else:
                self._send(404, "text/plain", b"sem placa")
        elif u.path == "/api/sequence":
            # retorna EXATAMENTE os quadros do agrupamento da placa de indice i
            i = int(q.get("i", [-1])[0])
            placas = ESTADO.get("placas") or []
            seq = placas[i].get("membros", []) if 0 <= i < len(placas) else []
            self._json({"seq": seq})
        elif u.path == "/api/mapa":
            pts = parse_gpx_pasta(ESTADO.get("folders") or [])
            placas = ESTADO.get("placas") or []
            marc = []
            if pts and placas:
                kms = [p["km"] for p in placas]
                kmin, kmax = min(kms), max(kms)
                rng = (kmax - kmin) or 1
                for i, p in enumerate(placas):
                    frac = (p["km"] - kmin) / rng
                    idx = min(len(pts) - 1, max(0, int(frac * (len(pts) - 1))))
                    marc.append({"i": i, "km": p["km"], "lado": p["lado"],
                                 "lat": pts[idx][0], "lon": pts[idx][1]})
            self._json({"track": pts[::5], "placas": marc})
        elif u.path == "/api/picker":
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.wm_attributes("-topmost", 1)
                pasta = filedialog.askdirectory(title="Selecionar pasta com imagens")
                root.destroy()
                self._json({"path": pasta or ""})
            except Exception as e:
                self._json({"path": "", "erro": str(e)})
        else:
            self._send(404, "text/plain", b"nao encontrado")

    # -------------------- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/run":
            brutas = [(f or "").strip().strip('"').strip("'") for f in (body.get("folders") or [])]
            brutas = [f for f in brutas if f]
            folders = [f for f in brutas if os.path.isdir(f)]
            if not folders:
                alvo = " | ".join(brutas) if brutas else "(nenhuma)"
                self._json({"ok": False, "msg": "Pasta nao encontrada: " + alvo}, 400); return
            if ESTADO["running"]:
                self._json({"ok": False, "msg": "ja esta rodando"}, 409); return
            imgsz = int(body.get("imgsz") or 1280); limite = int(body.get("limite") or 0)
            preset = {"conf": body.get("conf", 0.35), "min_quadros": body.get("min_quadros", 3),
                      "min_aspecto": body.get("min_aspecto", 0.45),
                      "max_aspecto": body.get("max_aspecto", 4.0),
                      "min_area": body.get("min_area", 0.0002), "lado": body.get("lado", "ambos")}
            PARAR["flag"] = False; PARAR["proc"] = None
            with LOCK:
                ESTADO.update({"running": True, "done": False, "error": None,
                               "step": "iniciando", "processed": 0, "total": 0,
                               "found": 0, "log": [], "placas": []})
            threading.Thread(target=worker, args=(folders, imgsz, limite, preset),
                             daemon=True).start()
            self._json({"ok": True})
        elif u.path == "/api/stop":
            PARAR["flag"] = True
            pr = PARAR.get("proc")
            if pr and pr.poll() is None:
                try:
                    pr.terminate()
                except Exception:
                    pass
            with LOCK:
                ESTADO.update({"running": False, "step": "interrompido", "eta_s": 0})
            self._json({"ok": True})
        elif u.path == "/api/clear":
            # limpa a tela (estado em memoria) p/ comecar outro projeto.
            # NAO apaga nada do disco: a rodada e a conferencia continuam salvas.
            if ESTADO["running"]:
                self._json({"ok": False, "msg": "ha uma deteccao rodando"}, 409); return
            with LOCK:
                ESTADO.update({"out": None, "folders": [], "placas": [], "done": False,
                               "running": False, "step": "ocioso", "processed": 0,
                               "total": 0, "found": 0, "preview": None})
            self._json({"ok": True})
        elif u.path == "/api/conf":
            # salva a conferencia (✓/✗) no servidor, dentro da pasta da rodada
            run = (body.get("run") or "").strip()
            if not run or not os.path.isdir(os.path.join(SAIDAS, run)):
                self._json({"ok": False, "msg": "rodada nao encontrada"}, 404); return
            atual = carregar_conf(run)
            nome = body.get("nome") or atual["nome"]
            marks = body.get("marks")
            if marks is None:
                marks = atual["marks"]
            ok = salvar_conf(run, nome, marks)
            self._json({"ok": ok, "nome": nome, "n_marks": len(marks)})
        elif u.path == "/api/run_delete":
            run = (body.get("run") or "").strip()
            alvo = os.path.join(SAIDAS, run)
            if not run or not dentro(SAIDAS, alvo) or not os.path.isdir(alvo):
                self._json({"ok": False, "msg": "rodada invalida"}, 400); return
            try:
                shutil.rmtree(alvo)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        elif u.path == "/api/replay":
            # conferencia AO VIVO: reproduz uma rodada do cache populando a lista em tempo real
            alvo = (body.get("out") or body.get("run") or "").strip()
            cand = alvo if os.path.isabs(alvo) else os.path.join(SAIDAS, alvo)
            if not alvo or not os.path.exists(os.path.join(cand, "deteccoes.csv")):
                self._json({"ok": False, "msg": "rodada nao encontrada: " + (alvo or "(vazio)")}, 404); return
            if ESTADO["running"]:
                self._json({"ok": False, "msg": "ja esta rodando"}, 409); return
            PARAR["flag"] = False; PARAR["proc"] = None
            with LOCK:
                ESTADO.update({"folders": body.get("folders") or []})
            args = (cand, float(body.get("conf", 0.35)), int(body.get("min_quadros", 3)),
                    float(body.get("min_aspecto", 0.45)), float(body.get("max_aspecto", 4.0)),
                    float(body.get("min_area", 0.0002)), body.get("lado", "ambos"),
                    float(body.get("pace", 0.01)))
            threading.Thread(target=replay_worker, args=args, daemon=True).start()
            self._json({"ok": True})
        elif u.path == "/api/salvar_referencias":
            out = ESTADO.get("out"); placas = ESTADO.get("placas") or []
            if not out or not placas:
                self._json({"ok": False, "msg": "rode a deteccao primeiro"}, 400); return
            n, pasta = 0, ""
            for i, p in enumerate(placas):
                c = gerar_referencia(out, p, i)
                if c:
                    n += 1; pasta = os.path.dirname(c)
            self._json({"ok": True, "n": n, "path": pasta})
        else:
            self._send(404, "text/plain", b"nao encontrado")


def main():
    os.makedirs(SAIDAS, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), H)
    url = f"http://127.0.0.1:{PORTA}/"
    print(f"\n  Painel de placas em: {url}")
    print("  (deixe esta janela aberta; feche para parar)\n")
    if not os.environ.get("PLACAS_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
