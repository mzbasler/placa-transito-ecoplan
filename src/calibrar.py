# -*- coding: utf-8 -*-
"""
Calibrador de limiares para o desafio: RECALL 100% das placas (melhor quadro:
mais proxima + inteira) com FALSO-POSITIVO < 15% (falsos / placas reais).

A deteccao pesada roda UMA vez (gera deteccoes.csv). Este script reusa as
funcoes deduplicar()/avaliar() do app.py e varre um grid de parametros em
segundos, listando as combinacoes que batem a meta.

Uso:
  python src/calibrar.py --out "saidas/PAINEL_xxx" --gabarito "dados/inventario_mt361.csv"
  python src/calibrar.py --out "..." --fp-max 15 --recall-min 100
"""
import argparse, os, sys, itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # importa ler_deteccoes, deduplicar, avaliar (NAO sobe o servidor)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="pasta com deteccoes.csv")
    ap.add_argument("--gabarito", default=os.path.join("dados", "inventario_mt361.csv"))
    ap.add_argument("--fp-max", type=float, default=15.0, help="FP%% maximo (falsos/placas reais)")
    ap.add_argument("--recall-min", type=float, default=100.0)
    ap.add_argument("--tol-m", type=float, default=30.0, help="tolerancia de casamento km (m)")
    ap.add_argument("--usar-lado", type=int, default=1, help="1=casa respeitando lado E/D, 0=ignora lado")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    dets = app.ler_deteccoes(args.out)
    if not dets:
        sys.exit(f"[erro] sem deteccoes.csv em {args.out}")
    print(f"[info] {len(dets)} deteccoes brutas carregadas de {args.out}")
    confs_raw = sorted(d["conf"] for d in dets)
    print(f"[info] conf bruta: min={confs_raw[0]:.3f} max={confs_raw[-1]:.3f} "
          f"mediana={confs_raw[len(confs_raw)//2]:.3f}")

    # grid de busca
    GRID = {
        "conf":        [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60],
        "min_quadros": [1, 2, 3, 4],
        "min_area":    [0.0, 0.00005, 0.0001, 0.0002, 0.0004, 0.0008],
        "min_asp":     [0.30, 0.40, 0.45],
        "max_asp":     [3.0, 4.0, 6.0],
    }
    usar_lado = bool(args.usar_lado)  # respeita lado (E/D) -> casamento mais honesto

    combos = list(itertools.product(
        GRID["conf"], GRID["min_quadros"], GRID["min_area"],
        GRID["min_asp"], GRID["max_asp"]))
    print(f"[info] varrendo {len(combos)} combinacoes (tol={args.tol_m:.0f} m, usar_lado={usar_lado})\n")

    resultados = []
    for conf, mq, ma, mnasp, mxasp in combos:
        placas = app.deduplicar(dets, conf, mq, mnasp, mxasp, ma, 0.06, "ambos")
        ev = app.avaliar(placas, args.gabarito, args.tol_m, usar_lado)
        if not ev or ev.get("erro") or ev.get("sem_overlap"):
            continue
        resultados.append({
            "conf": conf, "min_quadros": mq, "min_area": ma,
            "min_asp": mnasp, "max_asp": mxasp,
            "recall": ev["recall"], "fp_rel": ev["fp_rel"], "fp": ev["fp"],
            "achadas": ev["achadas"], "n_gt": ev["n_gt"], "n_det": ev["n_det"],
        })

    if not resultados:
        sys.exit("[erro] nenhuma combinacao avaliavel (sem overlap km imagens x gabarito?)")

    n_gt = resultados[0]["n_gt"]
    print(f"== overlap: {n_gt} placas reais no trecho coberto pelas imagens ==")
    print(f"== meta: recall >= {args.recall_min:.0f}%  e  FP <= {args.fp_max:.0f}%  "
          f"(<= {int(n_gt*args.fp_max/100)} falsos) ==\n")

    # 1) combinacoes que batem a meta, menor FP primeiro, depois menos detec. (mais limpo)
    metas = [r for r in resultados
             if r["recall"] >= args.recall_min and r["fp_rel"] <= args.fp_max]
    metas.sort(key=lambda r: (r["fp_rel"], r["n_det"], -r["conf"]))

    def linha(r):
        return (f"recall {r['recall']:5.1f}% | FP {r['fp_rel']:5.1f}% ({r['fp']:>2} fal) | "
                f"achou {r['achadas']:>3}/{r['n_gt']} | det {r['n_det']:>3} || "
                f"conf={r['conf']:.2f} min_quad={r['min_quadros']} "
                f"min_area={r['min_area']:.5f} asp=[{r['min_asp']:.2f},{r['max_asp']:.1f}]")

    if metas:
        print(f"### {len(metas)} combinacoes BATEM A META (melhores no topo):")
        for r in metas[:args.top]:
            print("  " + linha(r))
    else:
        print("### NENHUMA combinacao bateu a meta exata. Melhores por recall:")
        # maximo recall; entre eles, menor FP
        rmax = max(r["recall"] for r in resultados)
        quase = [r for r in resultados if r["recall"] >= rmax - 1e-9]
        quase.sort(key=lambda r: (r["fp_rel"], r["n_det"]))
        print(f"  (recall maximo atingivel = {rmax:.1f}%)")
        for r in quase[:args.top]:
            print("  " + linha(r))

    # 2) fronteira de Pareto recall x FP (referencia)
    print("\n### Fronteira recall x FP (cada recall, menor FP):")
    melhor_por_recall = {}
    for r in resultados:
        k = r["recall"]
        if k not in melhor_por_recall or r["fp_rel"] < melhor_por_recall[k]["fp_rel"]:
            melhor_por_recall[k] = r
    for k in sorted(melhor_por_recall, reverse=True)[:15]:
        print("  " + linha(melhor_por_recall[k]))

    # 3) diagnostico da melhor config: o que ela PERDE e quais sao os falsos
    escolha = (metas[0] if metas else
               max(resultados, key=lambda r: (r["recall"], -r["fp_rel"])))
    placas = app.deduplicar(dets, escolha["conf"], escolha["min_quadros"],
                            escolha["min_asp"], escolha["max_asp"],
                            escolha["min_area"], 0.06, "ambos")
    ev = app.avaliar(placas, args.gabarito, args.tol_m, usar_lado)
    print(f"\n### DIAGNOSTICO da config escolhida:\n  " + linha(escolha))
    falt = ev.get("faltaram", [])
    print(f"  placas PERDIDAS ({len(falt)}):")
    for g in falt:
        print(f"     km_foto {g['km']:.3f} lado {g.get('lado','')} {g.get('codigo','')}")
    fidx = ev.get("falsos_idx", [])
    print(f"  FALSOS-POSITIVOS ({len(fidx)}):")
    for i in fidx[:40]:
        p = placas[i]
        print(f"     km_foto {p['km']:.3f} lado {p['lado']} conf {p['conf']:.2f} "
              f"area {p['area_frac']:.5f} nq {p['n_quadros']}")


if __name__ == "__main__":
    main()
