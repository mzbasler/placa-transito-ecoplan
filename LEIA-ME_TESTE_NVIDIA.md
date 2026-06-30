# Branch de teste — modelo NVIDIA LocateAnything-3B

> Branch: `teste-locateanything-nvidia`. **Não mexe** no `main` (que continua com o YOLO `best.pt`).
> Objetivo: comparar o detector atual com o VLM de grounding **`nvidia/LocateAnything-3B`**.

## Arquitetura (split)

```
   ESTE PC (painel)                         PC do 4070 (servidor de inferência)
 ┌────────────────────┐   HTTP /detect    ┌──────────────────────────────────┐
 │ app.py (painel)    │  bytes da imagem  │ server_locateanything.py         │
 │  └ detectar_       │ ────────────────► │  └ carrega LocateAnything-3B      │
 │     locateanything │ ◄──────────────── │     (GPU, bf16) e devolve caixas  │
 │  lê as imagens da  │   JSON {boxes}    │                                  │
 │  rede \\192.168... │                   └──────────────────────────────────┘
 └────────────────────┘
```

- O **PC do 4070 só carrega o modelo** e devolve caixas. Não precisa enxergar o compartilhamento de imagens.
- **Este PC** continua lendo as imagens da rede e mostrando o painel — nada muda no fluxo de uso.

---

## Parte A — no PC do 4070 (via Remote Desktop / mstsc)

1. Instale **Python 3.10+** e crie um venv:
   ```bat
   cd C:\placas-servidor
   python -m venv venv
   venv\Scripts\activate
   ```
2. Copie a pasta **`servidor_modelo\`** desta branch para esse PC (ou faça `git clone` + `git checkout teste-locateanything-nvidia`).
3. Instale o PyTorch **com CUDA** (confira a versão de CUDA do driver; exemplo cu124):
   ```bat
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
   ```
4. Instale o resto:
   ```bat
   pip install -r servidor_modelo\requirements.txt
   ```
5. Suba o servidor (na 1ª vez ele **baixa ~7 GB do modelo** e demora a carregar):
   ```bat
   set PLACAS_SRV_PORT=8770
   python servidor_modelo\server_locateanything.py
   ```
6. Confirme que carregou abrindo no navegador do próprio 4070: `http://localhost:8770/health` → deve responder `{"ok": true, ...}`.
7. Descubra o **IP do 4070 na rede** (`ipconfig` → IPv4) e libere a **porta 8770 no Firewall do Windows** (entrada). Anote esse IP, ex.: `192.168.0.50`.

## Parte B — neste PC (o painel)

1. `git checkout teste-locateanything-nvidia`
2. Aponte o painel para o servidor (IP do 4070 + porta) e abra:
   ```bat
   set PLACAS_SERVIDOR=http://192.168.0.50:8770
   python app.py
   ```
   (ou edite o `.bat` para incluir o `set PLACAS_SERVIDOR=...` antes do `python app.py`.)
3. Use o painel normalmente: **Abrir pasta → Detectar**. As imagens vão uma a uma para o 4070; as caixas voltam e aparecem no painel como sempre.

---

## Diferenças importantes vs. o YOLO (leia antes de comparar)

| Tema | YOLO `best.pt` (main) | LocateAnything-3B (esta branch) |
|---|---|---|
| **Onde roda** | CPU, local | GPU do 4070, remoto |
| **Velocidade** | ~rápido/quadro | **VLM 3B: segundos por quadro** — bem mais lento. Use uma pasta pequena pro teste. |
| **Confiança por caixa** | sim (0–1) | **NÃO existe.** Cada caixa recebe score fixo `0.50`. O slider de confiança **não discrimina** nada aqui. |
| **Como ajustar qualidade** | limiar de conf + filtros | pelo **prompt** (`PLACAS_PROMPT`) e pelo **agrupamento** (min_quadros) |
| **Classe** | 1 classe `placa` (treinada) | open-vocabulary por texto (`"traffic sign"` por padrão) |

> Como não há confiança real, o **harness de calibração** (varrer limiares) perde sentido com este modelo: a métrica recall/FP deve ser comparada num ponto fixo, ajustando prompt e min_quadros — não o slider.

### Variáveis de ambiente úteis (servidor)
- `PLACAS_SRV_PORT` (8770) · `PLACAS_SCORE` (0.50) · `PLACAS_MAX_LADO` (1536, downscale p/ caber em 12 GB) · `PLACAS_GEN_MODE` (`hybrid`|`fast`|`slow`) · `PLACAS_PROMPT` no cliente (default `traffic sign`; ex.: `"traffic sign. speed limit sign. warning sign."`).

## ⚠️ Licença
O `nvidia/LocateAnything-3B` é distribuído sob **licença NVIDIA não-comercial** (pesquisa/acadêmico). Serve para **avaliar/comparar**, mas **não pode ser usado em produção comercial**. Para uso operacional na Ecoplan, manter o YOLO treinado (`main`) ou buscar um modelo com licença comercial.

## Como voltar ao YOLO
```bat
git checkout main
```
