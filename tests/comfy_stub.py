"""Faux ComfyUI pour les essais : un vrai serveur HTTP sur 127.0.0.1, aux formats de ComfyUI
0.35.1 relevés le 21/10/2026 dans son code (server.py, comfy_execution/jobs.py). Il joue les
graphes un par un, dans l'ordre de sa file : chaque SaveImage écrit un petit PNG sous
output/<filename_prefix>_00001_.png ; LoadImage exige un fichier présent sous input/.

    with ComfyStub(tmp_path) as comfy:
        comfy.url            # l'adresse à donner à l'adaptateur
        comfy.prompts        # les corps reçus par POST /prompt
        comfy.delay = 0.5    # secondes de « calcul » par graphe
        comfy.fail_next = "KSampler"   # le prochain graphe échoue sur ce nœud
"""
import email
import email.policy
import json
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.nodes import free_port

VERSION = "0.35.1"

DEFS = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sd_xl_base_1.0.safetensors",
                                              "sub/autre.safetensors"], {}]}},
        "input_order": {"required": ["ckpt_name"]}, "output": ["MODEL", "CLIP", "VAE"],
        "output_is_list": [False, False, False], "output_node": False,
        "python_module": "nodes", "category": "loaders"},
    "CLIPTextEncode": {
        "input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}},
        "input_order": {"required": ["text", "clip"]}, "output": ["CONDITIONING"],
        "output_is_list": [False], "output_node": False, "python_module": "nodes"},
    "EmptyLatentImage": {
        "input": {"required": {"width": ["INT", {"default": 512}], "height": ["INT", {"default": 512}],
                               "batch_size": ["INT", {"default": 1}]}},
        "input_order": {"required": ["width", "height", "batch_size"]}, "output": ["LATENT"],
        "output_is_list": [False], "output_node": False, "python_module": "nodes"},
    "KSampler": {
        "input": {"required": {"model": ["MODEL"], "seed": ["INT", {}], "steps": ["INT", {}],
                               "cfg": ["FLOAT", {}], "positive": ["CONDITIONING"],
                               "negative": ["CONDITIONING"], "latent_image": ["LATENT"]}},
        "input_order": {"required": ["model", "seed", "steps", "cfg", "positive", "negative",
                                     "latent_image"]},
        "output": ["LATENT"], "output_is_list": [False], "output_node": False,
        "python_module": "nodes"},
    "VAEDecode": {
        "input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}},
        "input_order": {"required": ["samples", "vae"]}, "output": ["IMAGE"],
        "output_is_list": [False], "output_node": False, "python_module": "nodes"},
    "LoadImage": {
        "input": {"required": {"image": [["exemple.png"], {"image_upload": True}]}},
        "input_order": {"required": ["image"]}, "output": ["IMAGE", "MASK"],
        "output_is_list": [False, False], "output_node": False, "python_module": "nodes"},
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
        "input_order": {"required": ["images", "filename_prefix"]}, "output": [],
        "output_is_list": [], "output_node": True, "python_module": "nodes"},
    "PreviewImage": {
        "input": {"required": {"images": ["IMAGE"]}}, "input_order": {"required": ["images"]},
        "output": [], "output_is_list": [], "output_node": True, "python_module": "nodes"},
}
MODELS = {"checkpoints": ["sd_xl_base_1.0.safetensors", "sub/autre.safetensors"],
          "loras": [], "vae": []}


def png(width=8, height=8, color=(200, 120, 40)) -> bytes:
    """Un vrai PNG, sans Pillow."""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class ComfyStub:
    def __init__(self, base: Path, delay: float = 0.0):
        self.base = Path(base)
        self.input = self.base / "input"
        self.output = self.base / "output"
        for folder in (self.input, self.output):
            folder.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.fail_next = None
        self.prompts = []           # corps de POST /prompt
        self.uploads = []           # (subfolder, nom, octets)
        self.cancelled = []         # identifiants annulés
        self.freed = 0
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._lock = threading.Lock()
        self._queue = []            # identifiants en attente
        self._running = None
        self._jobs = {}             # id -> {"status", "prompt", "error"}
        self._history = {}          # id -> entrée d'historique
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._interrupt = threading.Event()
        self._server = None

    # ----------------------------------------------------------- cycle de vie
    def __enter__(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                stub._get(self)

            def do_POST(self):
                stub._post(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._wake.set()
        self._server.shutdown()
        self._server.server_close()

    def status(self, run_id):
        with self._lock:
            job = self._jobs.get(run_id)
            return job["status"] if job else None

    # ---------------------------------------------------------------- calcul
    def _worker(self):
        while not self._stop.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            with self._lock:
                if not self._queue:
                    continue
                run_id = self._queue.pop(0)
                self._running = run_id
                self._jobs[run_id]["status"] = "in_progress"
                self._interrupt.clear()
                prompt = self._jobs[run_id]["prompt"]
                failing, self.fail_next = self.fail_next, None
            outcome = self._compute(run_id, prompt, failing)
            with self._lock:
                self._jobs[run_id].update(outcome)
                self._running = None
            self._wake.set()

    def _compute(self, run_id, prompt, failing):
        deadline = time.monotonic() + self.delay
        while time.monotonic() < deadline:
            if self._interrupt.wait(0.01):
                self._history[run_id] = {"prompt": [0, run_id, prompt, {}, []], "outputs": {},
                                         "status": {"status_str": "error", "completed": False,
                                                    "messages": [["execution_interrupted", {}]]}}
                return {"status": "cancelled"}
        if failing:
            node = next((n for n, v in prompt.items() if v["class_type"] == failing), None)
            error = {"node_id": node, "node_type": failing,
                     "exception_message": "CUDA out of memory (essai)"}
            self._history[run_id] = {"prompt": [0, run_id, prompt, {}, []], "outputs": {},
                                     "status": {"status_str": "error", "completed": False,
                                                "messages": [["execution_error", error]]}}
            return {"status": "failed", "error": error}
        outputs = {}
        for node_id, node in prompt.items():
            if node["class_type"] != "SaveImage":
                continue
            prefix = str(node["inputs"].get("filename_prefix") or "ComfyUI")
            sub, _, stem = prefix.rpartition("/")
            folder = self.output / sub
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{stem}_00001_.png"
            (folder / name).write_bytes(png())
            outputs[node_id] = {"images": [{"filename": name, "subfolder": sub, "type": "output"}]}
        self._history[run_id] = {"prompt": [0, run_id, prompt, {}, []], "outputs": outputs,
                                 "status": {"status_str": "success", "completed": True,
                                            "messages": []}}
        return {"status": "completed"}

    # ----------------------------------------------------------------- routes
    def _send(self, h, status, data=None, raw=None, kind="application/json"):
        body = raw if raw is not None else json.dumps(data).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", kind)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _body(self, h) -> bytes:
        return h.rfile.read(int(h.headers.get("Content-Length") or 0))

    def _get(self, h):
        parts = urlsplit(h.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == "/system_stats":
            return self._send(h, 200, {"system": {"os": "win32", "comfyui_version": VERSION,
                                                  "pytorch_version": "2.12.1+cu130",
                                                  "argv": ["main.py", "--port", str(self.port)]},
                                       "devices": [{"name": "cuda:0 Carte d'essai", "type": "cuda",
                                                    "vram_total": 8 * 2 ** 30,
                                                    "vram_free": 7 * 2 ** 30}]})
        if path == "/object_info":
            return self._send(h, 200, DEFS)
        if path.startswith("/object_info/"):
            cls = path.split("/", 2)[2]
            return self._send(h, 200, {cls: DEFS[cls]} if cls in DEFS else {})
        if path == "/models":
            return self._send(h, 200, list(MODELS))
        if path.startswith("/models/"):
            return self._send(h, 200, MODELS.get(path.split("/", 2)[2], []))
        if path.startswith("/api/jobs/"):
            run_id = path.split("/")[3]
            with self._lock:
                job = self._jobs.get(run_id)
                if job is None:
                    return self._send(h, 404, {"error": "Job not found"})
                out = {"id": run_id, "status": job["status"]}
                if job.get("error"):
                    out["execution_error"] = job["error"]
            return self._send(h, 200, out)
        if path.startswith("/history/"):
            run_id = path.split("/")[2]
            with self._lock:
                entry = self._history.get(run_id)
            return self._send(h, 200, {run_id: entry} if entry else {})
        if path == "/view":
            name = (query.get("filename") or [""])[0]
            sub = (query.get("subfolder") or [""])[0]
            kind = (query.get("type") or ["output"])[0]
            if "/" in name or ".." in name or ".." in sub:
                return self._send(h, 400, {})
            file = (self.output if kind == "output" else self.input) / sub / name
            if not file.is_file():
                return self._send(h, 404, {})
            return self._send(h, 200, raw=file.read_bytes(), kind="image/png")
        return self._send(h, 404, {})

    def _post(self, h):
        path = urlsplit(h.path).path
        raw = self._body(h)
        if path == "/upload/image":
            message = email.message_from_bytes(
                b"Content-Type: " + h.headers["Content-Type"].encode() + b"\r\n\r\n" + raw,
                policy=email.policy.HTTP)
            fields, data, name = {}, None, None
            for part in message.iter_parts():
                field = part.get_param("name", header="content-disposition")
                if field == "image":
                    data, name = part.get_payload(decode=True), part.get_filename()
                else:
                    fields[field] = part.get_content().strip()
            sub = fields.get("subfolder", "")
            folder = self.input / sub
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            self.uploads.append((sub, name, data))
            return self._send(h, 200, {"name": name, "subfolder": sub, "type": "input"})
        body = json.loads(raw or b"{}")
        if path == "/prompt":
            self.prompts.append(body)
            prompt, run_id = body.get("prompt"), body.get("prompt_id")
            unknown = [n for n, v in prompt.items() if v.get("class_type") not in DEFS]
            if unknown:
                cls = prompt[unknown[0]].get("class_type")
                return self._send(h, 400, {
                    "error": {"type": "missing_node_type",
                              "message": f"Node '{cls}' not found.", "details": "",
                              "extra_info": {}},
                    "node_errors": {}})
            for node_id, node in prompt.items():
                if node["class_type"] == "LoadImage":
                    if not (self.input / node["inputs"]["image"]).is_file():
                        return self._send(h, 400, {
                            "error": {"type": "prompt_outputs_failed_validation",
                                      "message": "Prompt outputs failed validation"},
                            "node_errors": {node_id: {"errors": [
                                {"type": "value_not_in_list", "message": "Value not in list",
                                 "details": f"image: '{node['inputs']['image']}'"}]}}})
            with self._lock:
                self._jobs[run_id] = {"status": "pending", "prompt": prompt}
                self._queue.append(run_id)
            self._wake.set()
            return self._send(h, 200, {"prompt_id": run_id, "number": len(self.prompts),
                                       "node_errors": {}})
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            run_id = path.split("/")[3]
            with self._lock:
                self.cancelled.append(run_id)
                if run_id in self._queue:
                    self._queue.remove(run_id)
                    self._jobs[run_id]["status"] = "cancelled"
                    return self._send(h, 200, {"cancelled": True})
                if self._running == run_id:
                    self._interrupt.set()
                    return self._send(h, 200, {"cancelled": True})
            return self._send(h, 200, {"cancelled": False})
        if path == "/history":
            with self._lock:
                for run_id in body.get("delete") or []:
                    self._history.pop(run_id, None)
            return self._send(h, 200, {})
        if path == "/free":
            self.freed += 1
            return self._send(h, 200, {})
        return self._send(h, 404, {})
