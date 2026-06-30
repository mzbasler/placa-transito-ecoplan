# -*- coding: utf-8 -*-
"""
Detector de placas que NAO roda o modelo localmente: ele manda cada imagem para um
SERVIDOR DE INFERENCIA na rede (o PC com a RTX 4070) que carrega o modelo da NVIDIA
'nvidia/LocateAnything-3B' e devolve as caixas. Este script roda nesta maquina (CPU)
e existe so' para falar com esse servidor e emitir EXATAMENTE a mesma saida do
detectar.py (YOLO), para o painel (app.py) nao precisar mudar nada:

  - <out>/deteccoes.csv : uma linha por deteccao (imagem, km, bbox, conf, area, completa)
  - <out>/crops/*.jpg   : recorte local de cada deteccao (painel rapido)
  - <out>/anotadas/*.jpg: quadro com a caixa desenhada (prova visual)
  - linhas '@DET\\t...'  : stream ao vivo lido pelo worker do app.py
  - linhas '[info]'/'[prog]' : progresso lido pelo worker do app.py

Contrato com o servidor (definido por nos, ver servidor_modelo/server_locateanything.py):
  POST  {servidor}/detect?conf=<float>&prompt=<texto-url>
        corpo = bytes crus da imagem (jpg/png/tif/...)
        resposta JSON = {"w": int, "h": int,
                         "boxes": [{"x1":float,"y1":float,"x2":float,"y2":float,"score":float}, ...]}
        coordenadas em PIXELS ABSOLUTOS da imagem original (a mesma que enviamos).
  GET   {servidor}/health -> 200 quando o modelo ja' esta' carregado.

Uso:
  python detectar_locateanything.py --pastas "PASTA1;PASTA2" --servidor http://192.168.0.50:8770 \
         --conf 0.10 --out "<dir>"
  python detectar_locateanything.py --lista arquivos.txt --servidor http://192.168.0.50:8770 --out "<dir>"
"""
import argparse, os, re, csv, sys, json, time, urllib.parse, urllib.request

def km_do_caminho(caminho):
    """Extrai km absoluto do nome (igual ao detectar.py: pega o ultimo decimal do nome)."""
    nome = os.path.basename(caminho)
    mo = re.findall(r"(\d+\.\d+)", nome)
    return round(float(mo[-1]), 3) if mo else 0.0

EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")
PROMPT_PADRAO = "traffic sign"   # placa de sinalizacao vertical (open-vocabulary)

def coleta_imagens(args):
    if args.lista:
        with open(args.lista, encoding="utf-8") as f:
            imgs = [l.strip() for l in f if l.strip()]
    else:
        imgs = []
        for pasta in args.pastas.split(";"):
            pasta = pasta.strip().strip('"').strip("'")
            if not pasta or not os.path.isdir(pasta):
                continue
            achados = [os.path.join(pasta, n) for n in os.listdir(pasta)
                       if n.lower().endswith(EXTS)]
            imgs.extend(sorted(achados))
    if args.stride > 1:
        imgs = imgs[::args.stride]
    if args.max:
        imgs = imgs[:args.max]
    return imgs


def checa_servidor(base, espera_s=180):
    """Espera o /health responder 200 (o modelo demora a carregar). Falha clara se nao subir."""
    url = base.rstrip("/") + "/health"
    t0 = time.time()
    while time.time() - t0 < espera_s:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(3)
    return False


def detecta_remoto(base, img_bytes, conf, prompt, timeout):
    """POST dos bytes da imagem -> JSON com as caixas. Devolve (w, h, boxes) ou levanta erro."""
    q = urllib.parse.urlencode({"conf": conf, "prompt": prompt})
    url = base.rstrip("/") + "/detect?" + q
    req = urllib.request.Request(url, data=img_bytes, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        dados = json.loads(r.read().decode("utf-8"))
    return int(dados["w"]), int(dados["h"]), dados.get("boxes", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pastas", default="")
    ap.add_argument("--lista", default="")
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--imgsz", type=int, default=1280)   # aceito p/ compat; o servidor controla a resolucao do modelo
    ap.add_argument("--servidor", default=os.environ.get("PLACAS_SERVIDOR", ""),
                    help="URL base do servidor de inferencia (ex.: http://192.168.0.50:8770)")
    ap.add_argument("--prompt", default=os.environ.get("PLACAS_PROMPT", PROMPT_PADRAO))
    ap.add_argument("--timeout", type=float, default=180.0, help="timeout por imagem (VLM e' lento)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--borda", type=int, default=12, help="margem px p/ considerar placa 'cortada'")
    args = ap.parse_args()

    if not args.servidor:
        sys.exit("[erro] informe --servidor http://IP:PORTA (ou defina PLACAS_SERVIDOR)")

    os.makedirs(os.path.join(args.out, "anotadas"), exist_ok=True)
    crops_dir = os.path.join(args.out, "crops")
    os.makedirs(crops_dir, exist_ok=True)

    imgs = coleta_imagens(args)
    print(f"[info] {len(imgs)} imagens a processar (stride={args.stride}, servidor={args.servidor})")
    if not imgs:
        sys.exit("[erro] nenhuma imagem encontrada")

    print(f"[info] aguardando o servidor de inferencia em {args.servidor} ...")
    if not checa_servidor(args.servidor):
        sys.exit(f"[erro] servidor de inferencia nao respondeu em {args.servidor}/health "
                 f"(verifique IP/porta, firewall e se o modelo terminou de carregar)")
    print(f"[info] servidor pronto. modelo: nvidia/LocateAnything-3B | prompt: '{args.prompt}'")

    import cv2, numpy as np

    csv_path = os.path.join(args.out, "deteccoes.csv")
    f = open(csv_path, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(f)
    w.writerow(["imagem", "km", "x1", "y1", "x2", "y2", "conf",
                "area_frac", "completa", "lado_img", "crop"])

    n_det = 0
    quadros_com_det = 0
    for i, caminho in enumerate(imgs):
        try:
            with open(caminho, "rb") as fb:
                img_bytes = fb.read()
        except Exception as e:
            print(f"[aviso] nao li {caminho}: {e}")
            continue
        try:
            W, H, boxes = detecta_remoto(args.servidor, img_bytes, args.conf,
                                         args.prompt, args.timeout)
        except Exception as e:
            print(f"[aviso] falha no servidor para {caminho}: {e}")
            continue
        km = km_do_caminho(caminho)
        if not boxes:
            if (i + 1) % 5 == 0:
                print(f"[prog] {i+1}/{len(imgs)} | placas ate agora: {n_det}")
            continue
        quadros_com_det += 1
        # decodifica localmente (mesmos bytes que o servidor viu -> coordenadas batem)
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            print(f"[aviso] nao decodifiquei {caminho}")
            continue
        Hi, Wi = img.shape[:2]
        for bx in boxes:
            x1 = max(0.0, min(float(bx["x1"]), Wi - 1))
            y1 = max(0.0, min(float(bx["y1"]), Hi - 1))
            x2 = max(0.0, min(float(bx["x2"]), Wi - 1))
            y2 = max(0.0, min(float(bx["y2"]), Hi - 1))
            if x2 <= x1 or y2 <= y1:
                continue
            conf = float(bx.get("score", 1.0))
            if conf < args.conf:
                continue
            area_frac = ((x2 - x1) * (y2 - y1)) / (Wi * Hi)
            completa = int(x1 > args.borda and y1 > args.borda and
                           x2 < Wi - args.borda and y2 < Hi - args.borda)
            cx = (x1 + x2) / 2
            lado_img = "E" if cx < Wi / 2 else "D"
            mx = int((x2 - x1) * 0.12) + 3
            my = int((y2 - y1) * 0.12) + 3
            cx1, cy1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
            cx2, cy2 = min(Wi, int(x2 + mx)), min(Hi, int(y2 + my))
            crop_path = os.path.join(crops_dir, f"d{n_det:05d}.jpg")
            recorte = img[cy1:cy2, cx1:cx2]
            if recorte.size:
                ok, buf = cv2.imencode(".jpg", recorte)
                if ok:
                    buf.tofile(crop_path)   # tofile tolera caminho Unicode no Windows
            w.writerow([caminho, km, int(x1), int(y1), int(x2), int(y2),
                        round(conf, 4), round(area_frac, 6), completa, lado_img,
                        crop_path])
            print("@DET\t" + "\t".join(str(v) for v in [
                km, int(x1), int(y1), int(x2), int(y2), round(conf, 4),
                round(area_frac, 6), completa, lado_img, caminho, crop_path]), flush=True)
            n_det += 1
            cor = (0, 200, 0) if completa else (0, 165, 255)
            cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), cor, 4)
            cv2.putText(img, f"{conf:.2f}", (int(x1), max(0, int(y1) - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, cor, 3)
        nome_out = f"km{km:08.3f}_{os.path.basename(caminho)}".replace(" ", "_")
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            buf.tofile(os.path.join(args.out, "anotadas", nome_out))
        if (i + 1) % 5 == 0:
            print(f"[prog] {i+1}/{len(imgs)} | quadros c/ placa: {quadros_com_det} | deteccoes: {n_det}")

    print(f"[prog] {len(imgs)}/{len(imgs)} | quadros c/ placa: {quadros_com_det} | deteccoes: {n_det}")
    f.close()
    print(f"[ok] FIM. {n_det} deteccoes em {quadros_com_det} quadros -> {csv_path}")


if __name__ == "__main__":
    main()
