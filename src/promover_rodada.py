# -*- coding: utf-8 -*-
"""
Promove uma rodada de deteccao (feita fora de saidas/) para dentro de saidas/,
para que o painel consiga servir os recortes (o painel so serve imagens dentro
de saidas/). Copia deteccoes.csv + crops/ e reaponta a coluna 'crop' do CSV.

Uso:
  python src/promover_rodada.py <src_out_dir> <nome_destino>
  ex.: python src/promover_rodada.py "...\\scratchpad\\cres_full_out" PAINEL_MT-361_CRESCENTE
"""
import os, sys, csv, shutil

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAIDAS = os.path.join(RAIZ, "saidas")


def main():
    if len(sys.argv) < 3:
        sys.exit("uso: promover_rodada.py <src_out_dir> <nome_destino>")
    src = os.path.abspath(sys.argv[1])
    nome = sys.argv[2]
    dest = os.path.join(SAIDAS, nome)
    src_csv = os.path.join(src, "deteccoes.csv")
    if not os.path.isfile(src_csv):
        sys.exit(f"[erro] sem deteccoes.csv em {src}")
    os.makedirs(dest, exist_ok=True)

    # copia crops/ (thumbs do painel)
    src_crops = os.path.join(src, "crops")
    dest_crops = os.path.join(dest, "crops")
    if os.path.isdir(src_crops):
        if os.path.isdir(dest_crops):
            shutil.rmtree(dest_crops)
        shutil.copytree(src_crops, dest_crops)
        print(f"[ok] crops copiados: {len(os.listdir(dest_crops))} arquivos")

    # reescreve deteccoes.csv reapontando a coluna crop p/ dest
    rows = list(csv.DictReader(open(src_csv, encoding="utf-8-sig")))
    n = 0
    for r in rows:
        c = r.get("crop") or ""
        if c:
            r["crop"] = os.path.join(dest_crops, os.path.basename(c))
            n += 1
    dest_csv = os.path.join(dest, "deteccoes.csv")
    if rows:
        with open(dest_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    print(f"[ok] {len(rows)} deteccoes -> {dest_csv} (crops reapontados: {n})")
    print(f"[ok] rodada pronta no painel como: {nome}")


if __name__ == "__main__":
    main()
