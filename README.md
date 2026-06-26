# Detector de Placas de Sinalização Vertical

Painel web local para detecção automática de placas de trânsito em imagens do PavScan. Usa um modelo **YOLO treinado** (`modelos/best.pt`, 1 classe: `placa`).

---

## Como funciona

1. Você aponta uma pasta com imagens (JPG, PNG, BMP, TIF, WEBP)
2. O modelo treinado (`modelos/best.pt`) detecta placas quadro a quadro
3. O sistema agrupa detecções da mesma placa física e mantém apenas o melhor quadro (mais próximo e completo)
4. Os resultados ficam disponíveis em tempo real no painel

---

## Requisitos

- Python 3.9+
- CPU (não requer GPU)

### Dependências Python

```bash
pip install ultralytics opencv-python openpyxl numpy
```

> O modelo treinado fica em `modelos/best.pt` (já incluído, ~22 MB). Não há download automático nem necessidade de GPU.

---

## Instalação

```bash
git clone https://github.com/mzbasler/placa-transito-ecoplan.git
cd placa-transito-ecoplan
pip install ultralytics opencv-python openpyxl numpy
```

---

## Como rodar

### Opção A — duplo clique
Abra o arquivo `INICIAR_PAINEL.bat`

### Opção B — terminal
```bash
python app.py
```

O painel abre automaticamente em `http://127.0.0.1:8765`

---

## Usando o painel

| Passo | O que fazer |
|---|---|
| **1. Abrir pasta** | Clique em `📁 Abrir ▾` → *Selecionar pasta…* (explorador nativo) ou *Pastas conhecidas* (lista da rede) |
| **2. Detectar** | Clique em `▶ Detectar` |
| **3. Acompanhar** | O painel esquerdo mostra progresso em tempo real |
| **4. Ver resultados** | O painel direito lista cada placa única com foto, km, lado e confiança |
| **5. Conferir** | Marque ✓ (placa real) ou ✗ (falso-positivo) para medir a precisão |
| **6. Referência** | Clique em 🔍 para abrir o quadro inteiro de onde saiu o recorte |
| **7. Exportar** | Botão *Salvar fotos de referência* grava os quadros anotados em `output/` |

---

## Estrutura do projeto

```
├── app.py                   # servidor HTTP + toda a lógica de negócio
├── INICIAR_PAINEL.bat       # atalho para Windows
├── modelos/
│   └── best.pt              # modelo YOLO treinado (1 classe: placa)
├── dados/
│   └── inventario_mt361.csv # gabarito de referência (151 placas reais, MT-361)
├── src/
│   ├── detectar.py          # engine de detecção (chamado como subprocess)
│   └── montagem.py          # geração de mosaico de recortes
└── web/
    ├── index.html           # frontend (painel)
    └── vendor/              # Leaflet.js (mapa)
```

Pastas geradas em runtime (ignoradas pelo git):

```
saidas/    # deteccoes.csv, recortes e imagens anotadas por rodada
output/    # fotos de referência com caixa marcada
```

---

## Configuração de rede

Por padrão o sistema tenta listar pastas no compartilhamento:

```
\\192.168.0.210\Setores\Setor Dev\_TESTES\Placas
```

Para alterar, edite a variável `ROOT_IMAGENS` no topo de `app.py`.

---

## Avaliação automática

O arquivo `dados/inventario_mt361.csv` contém 151 placas reais do MT-361 (km, lado, código CONTRAN). O painel usa esse gabarito para calcular **recall** e **falso-positivo** automaticamente quando há sobreposição de km entre as imagens e o inventário.
