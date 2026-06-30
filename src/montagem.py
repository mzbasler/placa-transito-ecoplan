# -*- coding: utf-8 -*-
"""
Monta um 'contact sheet' (mosaico) com o recorte de cada placa unica ja
deduplicada -- uma placa por celula, no quadro mais proximo e completo.
Serve para conferir rapidamente, num olhar, quantas sao placas de verdade
(verdadeiro-positivo) e quantas sao ruido (falso-positivo).

Uso:
  python montagem.py <placas_unicas.csv> <saida_dir> [--tile 220] [--cols 8] [--margem 0.15]
"""
import sys, csv, os, argparse, math
import cv2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("saida_dir")
    ap.add_argument("--tile", type=int, default=220)
    ap.add_argument("--cols", type=int, default=8)
    ap.add_argument("--margem", type=float, default=0.15)
    args = ap.parse_args()
    os.makedirs(args.saida_dir, exist_ok=True)

    with open(args.entrada, encoding="utf-8-sig") as f:
        placas = list(csv.DictReader(f))

    tiles = []
    for p in placas:
        crop = None
        # caminho rapido: usa o recorte local salvo na deteccao (sem reler a rede)
        cp = p.get("crop", "")
        if cp and os.path.exists(cp):
            crop = cv2.imread(cp)
        if crop is None or crop.size == 0:
            img = cv2.imread(p["imagem"])
            if img is None:
                continue
            H, W = img.shape[:2]
            x1, y1, x2, y2 = (int(float(p[k])) for k in ("x1", "y1", "x2", "y2"))
            mx = int((x2 - x1) * args.margem) + 4
            my = int((y2 - y1) * args.margem) + 4
            x1, y1 = max(0, x1 - mx), max(0, y1 - my)
            x2, y2 = min(W, x2 + mx), min(H, y2 + my)
            crop = img[y1:y2, x1:x2]
        if crop is None or crop.size == 0:
            continue
        t = cv2.resize(crop, (args.tile, args.tile))
        rotulo = f"km{float(p['km']):.2f} c{float(p['conf']):.2f}"
        cv2.rectangle(t, (0, args.tile - 22), (args.tile, args.tile), (0, 0, 0), -1)
        cv2.putText(t, rotulo, (4, args.tile - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        tiles.append(t)

    if not tiles:
        sys.exit("[erro] nenhum recorte gerado")

    cols = args.cols
    rows = math.ceil(len(tiles) / cols)
    import numpy as np
    sheet = np.full((rows * args.tile, cols * args.tile, 3), 40, dtype="uint8")
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet[r*args.tile:(r+1)*args.tile, c*args.tile:(c+1)*args.tile] = t

    out = os.path.join(args.saida_dir, "mosaico_placas.jpg")
    cv2.imwrite(out, sheet)
    print(f"[ok] {len(tiles)} placas no mosaico -> {out}")


if __name__ == "__main__":
    main()
