# -*- coding: utf-8 -*-
"""
SERVIDOR DE INFERENCIA — roda no PC com a RTX 4070 (Windows + GPU NVIDIA).

Carrega UMA vez o modelo 'nvidia/LocateAnything-3B' (VLM de grounding open-vocabulary:
MoonViT + Qwen2.5-3B) e expoe um HTTP simples que o painel (app.py, noutra maquina)
consome via src/detectar_locateanything.py:

  GET  /health
       -> 200 {"ok": true, "modelo": "..."} quando o modelo ja' carregou
       -> 503 enquanto carrega

  POST /detect?conf=<float>&prompt=<categoria-url>
       corpo = bytes crus da imagem (jpg/png/tif/...)
       resposta JSON = {"w": int, "h": int,
                        "boxes": [{"x1":float,"y1":float,"x2":float,"y2":float,"score":float}, ...]}
       coordenadas em PIXELS ABSOLUTOS da imagem ORIGINAL enviada.

IMPORTANTE (decisoes deste servidor):
  * O LocateAnything NAO devolve confianca por caixa (e' grounding sim/nao). Entao
    cada caixa recebe um score FIXO = SCORE_PADRAO (env PLACAS_SCORE, default 0.50).
    => O "slider de confianca" do painel NAO discrimina nada com este modelo; a
       qualidade se ajusta pelo PROMPT e pelo agrupamento (min_quadros), nao por limiar.
       0.50 fica abaixo do CONF_SOLO (0.70) do app.py de proposito, p/ que uma deteccao
       isolada NAO vire placa sozinha — o agrupamento por quadros continua mandando.
  * Imagens muito grandes sao reduzidas (lado maior <= MAX_LADO, env PLACAS_MAX_LADO,
    default 1536) so' para a inferencia caber nos 12 GB; como as coords vem normalizadas
    em [0,1000], elas sao reescaladas para a resolucao ORIGINAL — entao batem com os
    recortes/anotacoes que o cliente faz na imagem original.

Como rodar (no PC do 4070, dentro do venv com as deps de requirements.txt):
  set PLACAS_SRV_PORT=8770
  python server_locateanything.py
"""
import io, os, re, json, sys, time, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELO = os.environ.get("PLACAS_MODELO", "nvidia/LocateAnything-3B")
PORTA = int(os.environ.get("PLACAS_SRV_PORT", "8770"))
DEVICE = os.environ.get("PLACAS_DEVICE", "cuda")
SCORE_PADRAO = float(os.environ.get("PLACAS_SCORE", "0.50"))
MAX_LADO = int(os.environ.get("PLACAS_MAX_LADO", "1536"))
GEN_MODE = os.environ.get("PLACAS_GEN_MODE", "hybrid")   # fast | slow | hybrid
MAX_NEW = int(os.environ.get("PLACAS_MAX_NEW_TOKENS", "8192"))

_ESTADO = {"pronto": False, "erro": None}
_MODELO = {}            # guarda model/processor/tokenizer/dtype
_GPU_LOCK = threading.Lock()   # serializa a GPU (1 inferencia por vez)

# regex de uma caixa: <box><x1><y1><x2><y2></box> (inteiros ou floats), tolera espacos
_RE_BOX = re.compile(r"<box>(.*?)</box>", re.S)
_RE_NUM = re.compile(r"<\s*([0-9]+(?:\.[0-9]+)?)\s*>")


def carregar_modelo():
    """Carrega o modelo uma vez (thread em background). Marca _ESTADO['pronto']=True no fim."""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer, AutoProcessor
        dtype = torch.bfloat16
        print(f"[srv] carregando {MODELO} em {DEVICE} (bf16, sdpa)... isso demora.", flush=True)
        tok = AutoTokenizer.from_pretrained(MODELO, trust_remote_code=True)
        proc = AutoProcessor.from_pretrained(MODELO, trust_remote_code=True)
        model = AutoModel.from_pretrained(
            MODELO,
            torch_dtype=dtype,
            _attn_implementation="sdpa",   # OBRIGATORIO no Windows/4070 (default 'magi' e' Linux/Hopper)
            trust_remote_code=True,
        ).to(DEVICE).eval()
        _MODELO.update({"model": model, "proc": proc, "tok": tok, "dtype": dtype, "torch": torch})
        _ESTADO["pronto"] = True
        print(f"[srv] modelo pronto. ouvindo em 0.0.0.0:{PORTA}", flush=True)
    except Exception as e:
        _ESTADO["erro"] = str(e)
        print(f"[srv][ERRO] falha ao carregar modelo: {e}", flush=True)


def _reduz(img, max_lado):
    """Reduz a imagem (copia p/ a inferencia) se o lado maior passar de max_lado."""
    w, h = img.size
    m = max(w, h)
    if m <= max_lado:
        return img
    esc = max_lado / float(m)
    return img.resize((max(1, int(w * esc)), max(1, int(h * esc))))


def _extrai_texto(result):
    """generate() do LocateAnything devolve uma TUPLA (texto, tokens, info); pega o texto."""
    if isinstance(result, (tuple, list)):
        result = result[0] if result else ""
    if isinstance(result, (tuple, list)):   # text=[t] -> batch de 1
        result = result[0] if result else ""
    return result if isinstance(result, str) else str(result)


def _parse_boxes(texto, w, h):
    """Extrai caixas do texto do modelo. Coords normalizadas [0,1000] -> pixels (w,h)."""
    boxes = []
    for seg in _RE_BOX.findall(texto):
        nums = [float(n) for n in _RE_NUM.findall(seg)]
        if len(nums) < 4:          # 2 numeros = ponto; ignoramos (queremos caixas)
            continue
        x1, y1, x2, y2 = nums[:4]
        bx = {"x1": x1 / 1000.0 * w, "y1": y1 / 1000.0 * h,
              "x2": x2 / 1000.0 * w, "y2": y2 / 1000.0 * h,
              "score": SCORE_PADRAO}
        # normaliza ordem dos cantos
        if bx["x2"] < bx["x1"]:
            bx["x1"], bx["x2"] = bx["x2"], bx["x1"]
        if bx["y2"] < bx["y1"]:
            bx["y1"], bx["y2"] = bx["y2"], bx["y1"]
        boxes.append(bx)
    return boxes


def inferir(img_bytes, categoria):
    """Roda o modelo numa imagem -> (w, h, boxes em px da imagem ORIGINAL)."""
    from PIL import Image
    torch = _MODELO["torch"]
    model, proc, tok, dtype = _MODELO["model"], _MODELO["proc"], _MODELO["tok"], _MODELO["dtype"]

    original = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w0, h0 = original.size
    img = _reduz(original, MAX_LADO)

    # prompt no formato do demo oficial (open-vocabulary): "Locate all the instances..."
    instrucao = f"Locate all the instances that matches the following description: {categoria}."
    messages = [{"role": "user", "content": [
        {"type": "image", "image": img},
        {"type": "text", "text": instrucao}]}]

    text = proc.py_apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    imgs, vids = proc.process_vision_info(messages)
    inputs = proc(text=[text], images=imgs, videos=vids, return_tensors="pt").to(DEVICE)

    with _GPU_LOCK, torch.no_grad():
        result = model.generate(
            pixel_values=inputs["pixel_values"].to(dtype),
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            image_grid_hws=inputs.get("image_grid_hws", None),
            tokenizer=tok,
            max_new_tokens=MAX_NEW,
            use_cache=True,
            generation_mode=GEN_MODE,
            do_sample=False,
            verbose=False,
        )
    texto = _extrai_texto(result)
    # coords normalizadas -> escala pela resolucao ORIGINAL (independe do downscale)
    return w0, h0, _parse_boxes(texto, w0, h0)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            if _ESTADO["erro"]:
                self._json(500, {"ok": False, "erro": _ESTADO["erro"]})
            elif _ESTADO["pronto"]:
                self._json(200, {"ok": True, "modelo": MODELO})
            else:
                self._json(503, {"ok": False, "carregando": True})
        else:
            self._json(404, {"erro": "use GET /health ou POST /detect"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/detect":
            self._json(404, {"erro": "rota desconhecida"}); return
        if not _ESTADO["pronto"]:
            self._json(503, {"erro": "modelo ainda carregando"}); return
        q = urllib.parse.parse_qs(u.query)
        categoria = (q.get("prompt", ["traffic sign"])[0] or "traffic sign").strip()
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            self._json(400, {"erro": "corpo vazio (envie os bytes da imagem)"}); return
        img_bytes = self.rfile.read(n)
        try:
            t0 = time.time()
            w, h, boxes = inferir(img_bytes, categoria)
            dt = time.time() - t0
            print(f"[srv] {len(boxes)} caixa(s) em {dt:.1f}s ({w}x{h}) prompt='{categoria}'", flush=True)
            self._json(200, {"w": w, "h": h, "boxes": boxes})
        except Exception as e:
            print(f"[srv][ERRO] inferencia: {e}", flush=True)
            self._json(500, {"erro": str(e)})


def main():
    threading.Thread(target=carregar_modelo, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORTA), H)
    print(f"[srv] HTTP no ar em http://0.0.0.0:{PORTA} (aguarde o modelo carregar)", flush=True)
    print(f"[srv] teste: http://localhost:{PORTA}/health", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[srv] encerrando.", flush=True)


if __name__ == "__main__":
    main()
