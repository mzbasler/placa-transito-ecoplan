# -*- coding: utf-8 -*-
"""
Detector de placas usando um modelo YOLO TREINADO (modelos/best.pt, 1 classe: 'placa').
Roda em CPU (mais lento, mas funciona nesta maquina).

Saidas:
  - <out>/deteccoes.csv : uma linha por deteccao (imagem, km, bbox, conf, area, completa)
  - <out>/anotadas/*.jpg : quadros com deteccao desenhada (prova visual)

Uso:
  python detectar.py --pastas "PASTA1;PASTA2" --stride 5 --conf 0.04 --imgsz 1280 --out "<dir>"
  python detectar.py --lista arquivo_com_caminhos.txt --conf 0.04 --out "<dir>"
"""
import argparse, os, re, csv, glob, sys

def km_do_caminho(caminho):
    """Extrai km absoluto do nome. Os arquivos do PavScan ja usam km ABSOLUTO
    no nome (ex.: '...KM012 A KM024 12.482 1.Jpg' -> km 12.482), entao basta
    pegar o ultimo numero decimal do nome (nao somar o inicio da pasta)."""
    nome = os.path.basename(caminho)
    mo = re.findall(r"(\d+\.\d+)", nome)
    return round(float(mo[-1]), 3) if mo else 0.0

EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")

# modelo treinado (best.pt) ao lado do projeto: src/ -> ../modelos/best.pt
MODELO_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "modelos", "best.pt")

def coleta_imagens(args):
    if args.lista:
        with open(args.lista, encoding="utf-8") as f:
            imgs = [l.strip() for l in f if l.strip()]
    else:
        imgs = []
        for pasta in args.pastas.split(";"):
            pasta = pasta.strip().strip('"').strip("'")   # tolera aspas/espacos colados
            if not pasta or not os.path.isdir(pasta):
                continue
            # leitura direta (sem glob) p/ aceitar nomes com (), [], espacos etc.
            achados = [os.path.join(pasta, n) for n in os.listdir(pasta)
                       if n.lower().endswith(EXTS)]
            imgs.extend(sorted(achados))
    if args.stride > 1:
        imgs = imgs[::args.stride]
    if args.max:
        imgs = imgs[:args.max]
    return imgs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pastas", default="")
    ap.add_argument("--lista", default="")
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--modelo", default=MODELO_PADRAO)
    ap.add_argument("--out", required=True)
    ap.add_argument("--borda", type=int, default=12, help="margem px p/ considerar placa 'cortada'")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "anotadas"), exist_ok=True)
    imgs = coleta_imagens(args)
    print(f"[info] {len(imgs)} imagens a processar (stride={args.stride}, imgsz={args.imgsz})")
    if not imgs:
        sys.exit("[erro] nenhuma imagem encontrada")

    import cv2
    from ultralytics import YOLO
    if not os.path.isfile(args.modelo):
        sys.exit(f"[erro] modelo nao encontrado: {args.modelo}")
    modelo = YOLO(args.modelo)
    print(f"[info] modelo treinado carregado: {args.modelo} (classes: {modelo.names})")

    crops_dir = os.path.join(args.out, "crops")
    os.makedirs(crops_dir, exist_ok=True)

    csv_path = os.path.join(args.out, "deteccoes.csv")
    f = open(csv_path, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(f)
    w.writerow(["imagem", "km", "x1", "y1", "x2", "y2", "conf",
                "area_frac", "completa", "lado_img", "crop"])

    n_det = 0
    quadros_com_det = 0
    for i, caminho in enumerate(imgs):
        try:
            res = modelo.predict(caminho, conf=args.conf, imgsz=args.imgsz,
                                 verbose=False, device="cpu")[0]
        except Exception as e:
            print(f"[aviso] falha em {caminho}: {e}")
            continue
        H, W = res.orig_shape
        km = km_do_caminho(caminho)
        boxes = res.boxes
        if boxes is None or len(boxes) == 0:
            if (i + 1) % 5 == 0:
                print(f"[prog] {i+1}/{len(imgs)} | placas ate agora: {n_det}")
            continue
        quadros_com_det += 1
        img = cv2.imread(caminho)
        if img is None:
            continue
        for b in boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            conf = float(b.conf[0])
            area_frac = ((x2 - x1) * (y2 - y1)) / (W * H)
            completa = int(x1 > args.borda and y1 > args.borda and
                           x2 < W - args.borda and y2 < H - args.borda)
            cx = (x1 + x2) / 2
            lado_img = "E" if cx < W / 2 else "D"
            # recorte local (deixa mosaico/painel rapidos, sem reler a rede)
            mx = int((x2 - x1) * 0.12) + 3
            my = int((y2 - y1) * 0.12) + 3
            cx1, cy1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
            cx2, cy2 = min(W, int(x2 + mx)), min(H, int(y2 + my))
            crop_path = os.path.join(crops_dir, f"d{n_det:05d}.jpg")
            recorte = img[cy1:cy2, cx1:cx2]
            if recorte.size:
                cv2.imwrite(crop_path, recorte)
            w.writerow([caminho, km, int(x1), int(y1), int(x2), int(y2),
                        round(conf, 4), round(area_frac, 6), completa, lado_img,
                        crop_path])
            # emite a deteccao AO VIVO p/ o painel (worker agrupa em tempo real)
            print("@DET\t" + "\t".join(str(v) for v in [
                km, int(x1), int(y1), int(x2), int(y2), round(conf, 4),
                round(area_frac, 6), completa, lado_img, caminho, crop_path]), flush=True)
            n_det += 1
            cor = (0, 200, 0) if completa else (0, 165, 255)
            cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), cor, 4)
            cv2.putText(img, f"{conf:.2f}", (int(x1), max(0, int(y1) - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, cor, 3)
        if img is not None:
            nome_out = f"km{km:08.3f}_{os.path.basename(caminho)}".replace(" ", "_")
            cv2.imwrite(os.path.join(args.out, "anotadas", nome_out), img)
        if (i + 1) % 5 == 0:
            print(f"[prog] {i+1}/{len(imgs)} | quadros c/ placa: {quadros_com_det} | deteccoes: {n_det}")

    print(f"[prog] {len(imgs)}/{len(imgs)} | quadros c/ placa: {quadros_com_det} | deteccoes: {n_det}")
    f.close()
    print(f"[ok] FIM. {n_det} deteccoes em {quadros_com_det} quadros -> {csv_path}")

if __name__ == "__main__":
    main()
