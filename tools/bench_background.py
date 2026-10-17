"""Mesure ce que le travail de fond d'AIODOS coûte à la machine et à son utilisateur
(docs/arriere-plan.md).

Lance un nœud sans fenêtre dans un dossier de données jetable, lui fait indexer des
fichiers texte (par défaut une partie de la bibliothèque standard de Python, lue
seulement), et mesure pendant ce temps :
  - la charge du processeur : machine entière, cœurs performants (P) et efficaces (E),
    nœud AIODOS (et la part de ses threads à priorité abaissée), Ollama et ses
    llama-server ;
  - la carte graphique NVIDIA : utilisation et puissance (nvidia-smi) ;
  - la réactivité : délai avant le premier mot d'une conversation et vitesse de sa
    réponse, avec et sans bibliothèques ; temps de réponse de l'API de l'interface.

    python tools/bench_background.py --label avant --code CHEMIN   # une autre copie du code
    python tools/bench_background.py --label après --battery       # batterie simulée
    python tools/bench_background.py --set battery_mode=pause      # un réglage du nœud
    python tools/bench_background.py --json mesures.json           # résultat ajouté au fichier

Windows seulement (tools/cpu_probe.py). Ollama doit tourner, avec le modèle d'embedding
du nœud (embeddinggemma) et le modèle de conversation (--chat-model).
"""
import argparse
import ctypes
import html
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cpu_probe  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STDLIB = Path(os.__file__).resolve().parent
CORPUS = ["asyncio", "email", "xml", "unittest", "multiprocessing", "importlib", "http",
          "logging", "urllib", "html", "concurrent", "json", "re", "zipfile", "pathlib",
          "idlelib", "tkinter"]
QUESTION = "Explique en cinq phrases simples comment fonctionne une file de tâches."
SIMULATE_ENV = "AIODOS_SIMULATE_POWER"
OLLAMA_PROCESSES = {"ollama.exe", "llama-server.exe", "ollama app.exe"}
EVERY = 0.5


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _median(values):
    return round(statistics.median(values), 3) if values else None


def _mean(values):
    return round(statistics.fmean(values), 3) if values else None


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3)


# ----------------------------------------------------------------- le nœud
class Node:
    def __init__(self, code: Path, data: Path, simulate: str | None, slots: str | None):
        self.port = _free_port()
        self.data = data
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        env.pop(SIMULATE_ENV, None)
        if simulate:
            env[SIMULATE_ENV] = simulate
        args = [sys.executable, "-m", "aiodos", "--headless", "--no-migrate",
                "--data-dir", str(data), "--port", str(self.port)]
        if slots:
            args += ["--slots", slots]
        self.out = open(data / "sortie.txt", "w", encoding="utf-8")
        self.process = subprocess.Popen(args, cwd=code, stdout=self.out,
                                        stderr=subprocess.STDOUT, env=env)
        self.url = f"http://127.0.0.1:{self.port}"
        self.session = requests.Session()
        token = self._wait_ready()
        self.session.headers["X-Aiodos-Token"] = token

    def _wait_ready(self) -> str:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            text = (self.data / "sortie.txt").read_text(encoding="utf-8", errors="replace")
            found = re.search(r"prêt : http://\S+\?t=(\S+)", text)
            if found:
                return found.group(1)
            time.sleep(0.2)
        raise RuntimeError("Le nœud n'a pas démarré : voir " + str(self.data / "sortie.txt"))

    def api(self, method, path, body=None, timeout=30):
        r = self.session.request(method, self.url + path, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.out.close()


# ---------------------------------------------------------------- mesures
class Recorder:
    """Échantillons du processeur, de la carte graphique et de l'API, en continu."""

    def __init__(self, node: Node):
        self.node = node
        self.phase = "préparation"
        self.chatting = False
        self.cpu, self.gpu, self.api = [], [], []
        self._stop = threading.Event()
        pid = node.process.pid
        self.sampler = cpu_probe.Sampler({
            "aiodos": lambda table: cpu_probe.descendants({pid}, table),
            "ollama": lambda table: cpu_probe.descendants(
                {p for p, _, name in table if name.lower() in OLLAMA_PROCESSES}, table),
        }, watch_threads=pid)
        self._gpu_process = None
        self.models_seen = set()          # un modèle qui n'est pas d'ici : mesure à refaire
        self.battery = []
        samplers = [self._sample_cpu, self._probe_api, self._sample_gpu, self._watch_ollama]
        if unplugged():
            samplers.append(self._sample_battery)
        self._threads = [threading.Thread(target=t, daemon=True) for t in samplers]

    def start(self):
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        if self._gpu_process:
            self._gpu_process.terminate()
        for t in self._threads:
            t.join(5)

    def _tag(self) -> dict:
        return {"t": time.monotonic(), "phase": self.phase, "chat": self.chatting}

    def _sample_cpu(self):
        self.sampler.sample()
        while not self._stop.wait(EVERY):
            try:
                values = self.sampler.sample()
            except OSError:
                continue
            if values:
                self.cpu.append({**self._tag(), **values})

    def _probe_api(self):
        while not self._stop.wait(EVERY):
            started = time.perf_counter()
            try:
                self.node.api("GET", "/api/jobs", timeout=30)
            except requests.RequestException:
                continue
            self.api.append({**self._tag(), "ms": (time.perf_counter() - started) * 1000})

    def _sample_battery(self):
        """PC vraiment débranché : décharge de la batterie (mW), toutes les 5 s."""
        command = ["powershell", "-NoProfile", "-Command",
                   "(Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus)"
                   ".DischargeRate | Measure-Object -Sum | ForEach-Object Sum"]
        while not self._stop.wait(5):
            try:
                out = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout
                self.battery.append({**self._tag(), "mw": float(out.strip().replace(",", "."))})
            except (OSError, subprocess.SubprocessError, ValueError):
                continue

    def _watch_ollama(self):
        """Modèles chargés dans Ollama pendant la mesure : un autre programme qui s'en
        sert en même temps fausse les chiffres."""
        while not self._stop.wait(5):
            try:
                loaded = requests.get("http://127.0.0.1:11434/api/ps", timeout=5).json()
                self.models_seen.update(m["name"] for m in loaded.get("models", []))
            except (requests.RequestException, ValueError, KeyError):
                continue

    def _sample_gpu(self):
        try:
            self._gpu_process = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw",
                 "--format=csv,noheader,nounits", f"-lms={int(EVERY * 1000)}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            return
        for line in self._gpu_process.stdout:
            if self._stop.is_set():
                break
            parts = [p.strip() for p in line.split(",")]
            try:
                self.gpu.append({**self._tag(), "util": float(parts[0]),
                                 "watts": float(parts[1])})
            except (IndexError, ValueError):
                continue

    def summary(self, phase: str, chat: bool | None = None) -> dict:
        def keep(rows):
            return [r for r in rows if r["phase"] == phase and (chat is None or r["chat"] == chat)]
        cpu, gpu, api = keep(self.cpu), keep(self.gpu), keep(self.api)
        out = {"samples": len(cpu)}
        battery = keep(self.battery)
        if battery:
            out["battery_w"] = _mean([r["mw"] / 1000 for r in battery])
        for key in ("system", "cores_P", "cores_E", "aiodos", "ollama", "threads_lowered",
                    "threads_normal"):
            values = [r[key] for r in cpu if key in r]
            if values:
                out[key] = _mean(values)
        if cpu and "threads_lowered_count" in cpu[-1]:
            out["threads_lowered_count"] = max(r["threads_lowered_count"] for r in cpu)
        if gpu:
            out["gpu_util"] = _mean([r["util"] for r in gpu])
            out["gpu_watts"] = _mean([r["watts"] for r in gpu])
        if api:
            latencies = [r["ms"] for r in api]
            out["api_ms_median"] = _median(latencies)
            out["api_ms_p95"] = _p95(latencies)
            out["api_ms_max"] = round(max(latencies), 1)
        return out


# ------------------------------------------------------------ conversation
def chat(node: Node, recorder: Recorder, model: str, libraries: bool) -> dict:
    recorder.chatting = True
    started = time.perf_counter()
    first = None
    final = ""
    error = None
    try:
        with node.session.post(node.url + "/api/chat", stream=True, timeout=(10, 600), json={
                "text": QUESTION, "model": model, "libraries": libraries,
                "conversations": False, "think": False}) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event["type"] == "render" and first is None:
                    first = time.perf_counter()
                if event["type"] == "done":
                    final = event.get("html") or ""
                    error = event.get("error")
                    break
    finally:
        recorder.chatting = False
    done = time.perf_counter()
    text = html.unescape(re.sub(r"<[^>]+>", "", final))
    speed = len(text) / (done - first) if first and done > first else None
    return {"libraries": libraries, "ttft_s": round(first - started, 3) if first else None,
            "total_s": round(done - started, 3), "chars": len(text),
            "chars_per_s": round(speed, 1) if speed else None, "error": error}


def chat_summary(chats: list) -> dict:
    out = {}
    for libraries in (False, True):
        mine = [c for c in chats if c["libraries"] == libraries and not c["error"]]
        key = "avec_bibliotheques" if libraries else "sans_bibliotheques"
        out[key] = {"n": len(mine),
                    "ttft_s_median": _median([c["ttft_s"] for c in mine if c["ttft_s"]]),
                    "chars_per_s_median": _median([c["chars_per_s"] for c in mine
                                                   if c["chars_per_s"]])}
    return out


# --------------------------------------------------------------- scénario
def big_files(folder: Path, count: int, size: int = 1_500_000) -> list:
    """`count` gros fichiers texte (un livre, un long journal…), faits des sources de la
    bibliothèque standard : une seule unité de travail pour des centaines de lots."""
    sources = sorted(STDLIB.glob("*.py"))
    files, index = [], 0
    for n in range(count):
        parts, length = [], 0
        while length < size:
            text = sources[index % len(sources)].read_text(encoding="utf-8", errors="ignore")
            parts.append(text)
            length += len(text)
            index += 1
        path = folder / f"gros-{n + 1}.txt"
        path.write_text("".join(parts)[:size], encoding="utf-8")
        files.append(path)
    return files


def unplugged() -> bool:
    """Le PC est-il vraiment sur batterie (GetSystemPowerStatus) ?"""
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved1", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    status = PowerStatus()
    return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
                and status.ACLineStatus == 0)


def wait_job(node: Node, job_id: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = next((j for j in node.api("GET", "/api/jobs")["jobs"] if j["id"] == job_id), None)
        # Le résumé (passages…) s'écrit juste après la fin de la dernière unité
        if job and job["status"] in ("failed", "cancelled"):
            return job
        if job and job["status"] == "done" and "chunks" in (job.get("result") or {}):
            return job
        time.sleep(1)
    node.api("POST", f"/api/jobs/{job_id}/cancel")
    raise RuntimeError(f"Indexation non terminée après {timeout:.0f} s : annulée.")


def machine() -> dict:
    info = {"cpu": None, "gpu": None, "ollama": None, "power": None}
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        info["cpu"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        pass
    try:
        info["gpu"] = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=10,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        info["ollama"] = requests.get("http://127.0.0.1:11434/api/version", timeout=5).json()["version"]
    except (requests.RequestException, ValueError, KeyError):
        pass
    try:
        sys.path.insert(0, str(REPO))
        from aiodos.node import power
        info["power"] = power.read()
    except Exception:
        pass
    return info


def revision(code: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(code), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run(args) -> dict:
    code = Path(args.code).resolve()
    data = Path(tempfile.mkdtemp(prefix="aiodos-mesure-"))
    simulate = "battery" if args.battery else None
    node = Node(code, data, simulate, args.slots)
    recorder = Recorder(node)
    # Pas de chemin local dans les résultats (nom d'utilisateur, dossiers de travail)
    result = {"label": args.label, "code": "ce dépôt" if code == REPO else code.name,
              "revision": revision(code),
              "simulation": simulate, "settings": dict(args.set), "slots": args.slots,
              "corpus": f"{args.big} gros fichiers" if args.big else args.corpus,
              "machine": machine(), "chat_model": args.chat_model, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        node.api("PUT", "/api/settings", {"media_vision": False, "media_transcribe": False,
                                          "think": False, "conversations_enabled": False,
                                          **dict(args.set)})
        recorder.start()
        print("Préparation : modèles chargés par le nœud…", flush=True)
        warm = data / "chauffe.txt"
        warm.write_text("Texte de mise en route.\n" * 40, encoding="utf-8")
        collection = node.api("POST", "/api/collections", {"name": "Mesure"})["id"]
        job = node.api("POST", "/api/index", {"paths": [str(warm)], "collection_id": collection})
        wait_job(node, job["job_id"], 300)
        chat(node, recorder, args.chat_model, libraries=False)

        recorder.phase = "repos"
        print("Au repos…", flush=True)
        time.sleep(args.idle)
        idle_chats = [chat(node, recorder, args.chat_model, libraries=lib) for lib in (False, True)]
        time.sleep(2)

        recorder.phase = "indexation"
        if args.big:
            paths = [str(p) for p in big_files(data, args.big)]
            print(f"Indexation de {len(paths)} gros fichiers…", flush=True)
        else:
            paths = [str(p) for p in (Path(n) if Path(n).is_absolute() else STDLIB / n
                                      for n in args.corpus) if p.exists()]
            print(f"Indexation de {len(paths)} dossiers ({', '.join(args.corpus)})…", flush=True)
        started = time.monotonic()
        job_id = node.api("POST", "/api/index", {"paths": paths, "collection_id": collection})["job_id"]
        busy_chats = []
        time.sleep(args.chat_delay)
        for i in range(args.chats):
            current = next(j for j in node.api("GET", "/api/jobs")["jobs"] if j["id"] == job_id)
            if current["status"] not in ("queued", "running"):
                break
            busy_chats.append(chat(node, recorder, args.chat_model, libraries=bool(i % 2)))
            print(f"  conversation {i + 1} : {busy_chats[-1]}", flush=True)
            time.sleep(args.chat_gap)
        job = wait_job(node, job_id, args.timeout)
        duration = time.monotonic() - started
        recorder.phase = "fin"
        chunks = (job.get("result") or {}).get("chunks") or 0
        result.update({
            "index": {"duration_s": round(duration, 1), "files": job["done"],
                      "failed": job["failed"], "skipped": job["skipped"], "chunks": chunks,
                      "chunks_per_s": round(chunks / duration, 1) if duration else None},
            "idle": recorder.summary("repos", chat=False),
            "idle_chat": recorder.summary("repos", chat=True),
            "indexing_alone": recorder.summary("indexation", chat=False),
            "indexing_chat": recorder.summary("indexation", chat=True),
            "chats_idle": idle_chats, "chats_indexing": busy_chats,
            "chat_idle_summary": chat_summary(idle_chats),
            "chat_indexing_summary": chat_summary(busy_chats),
            "ollama_models_seen": sorted(recorder.models_seen),
        })
    finally:
        recorder.stop()
        node.stop()
        if not args.keep:
            shutil.rmtree(data, ignore_errors=True)
    return result


def show(result: dict):
    print(f"\n=== {result['label']} ({result['revision'] or result['code']})"
          f"{' · batterie simulée' if result['simulation'] else ''}")
    index = result["index"]
    print(f"Indexation : {index['files']} fichiers, {index['chunks']} passages en "
          f"{index['duration_s']} s ({index['chunks_per_s']} passages/s)")
    for name in ("idle", "idle_chat", "indexing_alone", "indexing_chat"):
        print(f"  {name:15} {result[name]}")
    print(f"  conversations au repos     : {result['chat_idle_summary']}")
    print(f"  conversations en indexation : {result['chat_indexing_summary']}")
    print(f"  modèles vus dans Ollama     : {', '.join(result['ollama_models_seen'])}")


def _setting(text: str) -> tuple:
    key, _, value = text.partition("=")
    try:
        return key, json.loads(value)
    except ValueError:
        return key, value


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="mesure")
    parser.add_argument("--code", default=str(REPO), help="dossier du code d'AIODOS à lancer")
    parser.add_argument("--battery", action="store_true",
                        help=f"batterie simulée pour le nœud ({SIMULATE_ENV}=battery)")
    parser.add_argument("--set", action="append", default=[], metavar="CLÉ=VALEUR",
                        type=_setting, help="réglage du nœud (valeur JSON ou texte)")
    parser.add_argument("--slots", default=None, help="option --slots du nœud")
    parser.add_argument("--corpus", nargs="+", default=CORPUS,
                        help=f"dossiers à indexer : chemins, ou noms de dossiers de {STDLIB}")
    parser.add_argument("--big", type=int, default=0, metavar="N",
                        help="indexer N gros fichiers (1,5 Mo chacun) au lieu des dossiers")
    parser.add_argument("--chat-model", default="qwen2.5:7b")
    parser.add_argument("--chats", type=int, default=6, help="conversations pendant l'indexation")
    parser.add_argument("--chat-delay", type=float, default=8.0)
    parser.add_argument("--chat-gap", type=float, default=4.0)
    parser.add_argument("--idle", type=float, default=10.0, help="secondes de mesure au repos")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--json", default=None, help="fichier où ajouter le résultat")
    parser.add_argument("--keep", action="store_true", help="garder le dossier de données")
    args = parser.parse_args()
    result = run(args)
    show(result)
    if args.json:
        path = Path(args.json)
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        previous.append(result)
        path.write_text(json.dumps(previous, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
