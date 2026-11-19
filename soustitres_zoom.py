#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
soustitres_zoom.py — Sous-titres traduits en direct pour Zoom,
avec Gemini 3.5 Live Translate.

Le script écoute deux sources audio :
  • votre micro                         → ce que VOUS dites
  • la sortie audio de Zoom (BlackHole) → ce que disent les PARTICIPANTS

Il envoie le son en continu à Gemini 3.5 Live Translate, qui traduit au fil de
la parole (français ⇄ anglais par défaut). La traduction s'affiche dans un
bandeau toujours au premier plan, en bas de l'écran. Partagez votre ÉCRAN
ENTIER dans Zoom : tout le monde voit les sous-titres, sans rien installer.

Sans casque, le micro capte aussi la voix des participants qui sort des
haut-parleurs. L'anti-écho coupe le micro pendant que Zoom diffuse
quelqu'un : on sait exactement quand, puisque le son de Zoom arrive aussi
par BlackHole.

Installation et réglages : voir LISEZMOI.md.
Arrêt : Ctrl+C dans le terminal, ou double-clic sur le bandeau.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import queue
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from math import gcd, log10

import numpy as np

SR = 16_000                 # format attendu par Gemini : PCM 16 bits, 16 kHz, mono
BLOCK_S = 0.1               # Gemini recommande des paquets de 100 ms
DEFAULT_MODEL = "gemini-3.5-live-translate-preview"


def rms_db(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    return 20 * log10(max(float(np.sqrt(np.mean(x * x))), 1e-6))


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def tail(text: str, n: int) -> str:
    """Garde la fin d'un texte (≈ n caractères), coupée sur un mot."""
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    cut = t.find(" ", len(t) - n)
    return "…" + (t[cut + 1:] if cut != -1 else t[-n:])


# --------------------------------------------------------------------------
# Anti-écho
# --------------------------------------------------------------------------
class EchoGate:
    """Coupe le micro quand Zoom diffuse un participant.

    Le son de Zoom (BlackHole) sert de référence : c'est exactement ce qui sort
    des haut-parleurs. Le micro est retardé de `delay_s` pour que la décision
    sur chaque paquet tienne compte de la référence à la même période.
    Le micro est coupé de `lookahead_s` avant le début du son jusqu'à
    `hold_s` après la fin (pour couvrir la réverbération de la pièce).

    Avec `double_talk_db`, le micro reste ouvert si votre voix couvre
    nettement l'écho. Le niveau d'écho attendu est appris pendant que les
    participants parlent seuls.
    """

    def __init__(self, ref_threshold_db=-55.0, hold_s=0.5, lookahead_s=0.2, double_talk_db=None):
        self.ref_thr = ref_threshold_db
        self.hold = hold_s
        self.lookahead = lookahead_s
        self.delay_s = lookahead_s + 0.05
        self.double_talk_db = double_talk_db
        self.ref = deque(maxlen=100)            # (t, dB) des 10 dernières secondes
        self.coupling = deque(maxlen=300)       # écarts micro − référence (dB) en écho seul
        self.lock = threading.Lock()

    def update_ref(self, t: float, db: float):
        with self.lock:
            self.ref.append((t, db))

    def allow(self, t: float, mic_db: float) -> bool:
        """Le paquet micro capté à l'instant t peut-il partir ?"""
        with self.lock:
            window = [db for (rt, db) in self.ref
                      if t - self.hold <= rt <= t + self.lookahead]
            active = [db for db in window if db > self.ref_thr]
            if not active:
                return True
            diff = mic_db - max(active)
            if self.double_talk_db is not None and len(self.coupling) >= 20:
                if diff > float(np.median(self.coupling)) + self.double_talk_db:
                    return True                 # vous parlez par-dessus : on laisse passer
            self.coupling.append(diff)
            return False


class MicProcessor:
    """Retarde le micro, applique l'anti-écho et envoie les paquets."""

    def __init__(self, gate: EchoGate | None, emit):
        self.gate = gate
        self.emit = emit
        self.buf = deque()
        self.gated = False                      # état courant (pour --test-audio)

    def on_block(self, t: float, x: np.ndarray):
        if self.gate is None:
            self.emit(x)
            return
        self.buf.append((t, x))
        while self.buf and self.buf[0][0] <= t - self.gate.delay_s:
            bt, bx = self.buf.popleft()
            ok = self.gate.allow(bt, rms_db(bx))
            self.gated = not ok
            self.emit(bx if ok else np.zeros_like(bx))   # silence à la place de l'écho


# --------------------------------------------------------------------------
# Capture audio
# --------------------------------------------------------------------------
def find_device(spec):
    import sounddevice as sd
    devices = sd.query_devices()
    if spec is None:
        idx = sd.default.device[0]
        if idx is None or idx < 0:
            raise SystemExit("Aucun micro par défaut. Précisez-le avec --mic (voir --list-devices).")
        return idx, devices[idx]
    if str(spec).isdigit():
        idx = int(spec)
        return idx, devices[idx]
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and str(spec).lower() in d["name"].lower():
            return i, d
    raise SystemExit(f"Périphérique d'entrée introuvable : « {spec} ». "
                     "Lancez le script avec --list-devices pour voir la liste.")


class Capture:
    """Ouvre une entrée audio et appelle on_block(t, float32 mono 16 kHz) toutes les 100 ms."""

    def __init__(self, spec, on_block):
        import sounddevice as sd
        idx, info = find_device(spec)
        self.name = info["name"]
        # On ouvre l'entrée à sa fréquence native et on rééchantillonne nous-mêmes :
        # demander 16 kHz à BlackHole peut perturber le périphérique à sorties multiples.
        sr = int(info["default_samplerate"])
        self.sr = sr
        g = gcd(SR, sr)
        up, down = SR // g, sr // g

        def callback(indata, frames, ti, status):
            x = indata[:, 0].copy()
            if sr != SR:
                from scipy.signal import resample_poly
                x = resample_poly(x, up, down).astype(np.float32)
            on_block(time.monotonic(), x)

        self.stream = sd.InputStream(device=idx, channels=1, samplerate=sr, dtype="float32",
                                     blocksize=int(sr * BLOCK_S), callback=callback)

    def start(self):
        self.stream.start()

    def close(self):
        self.stream.stop()
        self.stream.close()


# --------------------------------------------------------------------------
# Assemblage des sous-titres
# --------------------------------------------------------------------------
@dataclass
class Caption:
    id: int
    label: str
    original: str = ""
    translation: str = ""
    t: float = 0.0          # dernière mise à jour (horloge murale, pour l'affichage)
    final: bool = False
    lang: str = ""          # langue de la traduction (pour la couleur)


_ids = itertools.count(1)


class Assembler:
    """Regroupe les morceaux de texte de Gemini en répliques.

    Une réplique se termine après `gap_s` secondes sans nouveau texte.
    """

    def __init__(self, label, ui_q, final_q, gap_s):
        self.label, self.ui_q, self.final_q, self.gap = label, ui_q, final_q, gap_s
        self.cur: Caption | None = None
        self.last = 0.0
        self.lock = threading.Lock()

    def add(self, kind: str, text: str, lang: str = ""):
        if not text:
            return
        with self.lock:
            now = time.monotonic()
            # Nouvelle réplique après un silence, ou quand la langue de la
            # traduction change (on passe de l'anglais au français, par ex.).
            lang_changed = (kind == "out" and self.cur is not None
                            and self.cur.lang and lang and lang != self.cur.lang)
            if self.cur is None or now - self.last > self.gap or lang_changed:
                self._finalize()
                self.cur = Caption(next(_ids), self.label)
            if kind == "in":
                self.cur.original += text
            else:
                self.cur.translation += text
                if lang:
                    self.cur.lang = lang
            self.cur.t = time.time()
            self.last = now
            self.ui_q.put(replace(self.cur))

    def flush(self):
        with self.lock:
            self._finalize()

    def tick(self):
        with self.lock:
            if self.cur is not None and time.monotonic() - self.last > self.gap:
                self._finalize()

    def _finalize(self):
        if self.cur is not None and not self.cur.final:
            self.cur.final = True
            if self.cur.original.strip() or self.cur.translation.strip():
                self.final_q.put(replace(self.cur))
        self.cur = None


class Finalizer(threading.Thread):
    """Écrit les répliques terminées (terminal, fichier, Zoom)."""

    def __init__(self, assemblers, final_q, zoom, save_path, stop):
        super().__init__(daemon=True)
        self.assemblers, self.final_q, self.zoom, self.stop = assemblers, final_q, zoom, stop
        self.save = open(save_path, "a", encoding="utf-8") if save_path else None

    def run(self):
        while True:  # thread démon : il s'arrête avec le programme
            for a in self.assemblers:
                a.tick()
            try:
                c = self.final_q.get(timeout=0.3)
            except queue.Empty:
                continue
            orig = " ".join(c.original.split())
            trans = " ".join(c.translation.split())
            line = f"[{time.strftime('%H:%M:%S')}] {c.label} : {orig}\n           ⇒ {trans}"
            print(line, flush=True)
            if self.save:
                self.save.write(line + "\n")
                self.save.flush()
            if self.zoom and trans:
                try:
                    self.zoom.send(f"{c.label} : {trans}" if c.label else trans)
                except Exception as e:
                    print(f"  ⚠ Envoi à Zoom impossible : {e}")


# --------------------------------------------------------------------------
# Gemini 3.5 Live Translate
# --------------------------------------------------------------------------
class GeminiBridge:
    """Une session Gemini par source et par langue cible.

    Chaque session a UNE langue cible et reste muette quand on parle déjà
    cette langue (echo_target_language=False). Avec fr,en, il y a donc deux
    sessions par source : l'une traduit vers le français, l'autre vers
    l'anglais, et une seule des deux répond à chaque phrase.
    """

    def __init__(self, model, langs, assemblers_by_source, labels, stop, api_version=None):
        self.model, self.langs = model, langs
        self.api_version = api_version
        self.assemblers = assemblers_by_source
        self.labels = labels
        self.stop = stop
        self.loop = asyncio.new_event_loop()
        self.queues: dict[tuple[str, int], asyncio.Queue] = {}
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        self.ready.wait(timeout=30)

    def push(self, source: str, x: np.ndarray):
        """Appelé depuis les threads audio."""
        if not self.ready.is_set():
            return
        pcm = to_pcm16(x)
        for i in range(len(self.langs)):
            q = self.queues.get((source, i))
            if q is not None:
                self.loop.call_soon_threadsafe(self._put, q, pcm)

    @staticmethod
    def _put(q: asyncio.Queue, item):
        if q.full():                       # on ne garde que les 5 dernières secondes
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(item)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        from google import genai
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            print("❌ Variable GEMINI_API_KEY manquante (voir LISEZMOI.md).")
            self.stop.set()
            self.ready.set()
            return
        print(f"google-genai {genai.__version__} — vérification du modèle {self.model}…")
        # Le SDK vise l'API « v1 » par défaut, mais les modèles en preview comme
        # Live Translate ne sont publiés que sur « v1beta ». On essaie v1beta,
        # puis v1alpha, puis Vertex AI (mode express).
        versions = [self.api_version] if self.api_version else ["v1beta", "v1alpha"]
        attempts = [(f"API Gemini {v}", {"http_options": {"api_version": v}}) for v in versions]
        attempts.append(("Vertex AI (mode express)", {"vertexai": True}))
        ok = False
        for mode, kwargs in attempts:
            try:
                self.client = genai.Client(api_key=key, **kwargs)
            except Exception as e:
                print(f"   {mode} : client impossible à créer ({e})")
                continue
            if await self._preflight(mode):
                ok = True
                break
        if not ok:
            print("❌ Aucune voie ne donne accès au modèle. "
                  "Voir la section « Erreur HTTP 404 » de LISEZMOI.md.")
            self.stop.set()
            self.ready.set()
            return
        tasks = []
        for source in self.assemblers:
            for i, tgt in enumerate(self.langs):
                q = asyncio.Queue(maxsize=50)
                self.queues[(source, i)] = q
                tasks.append(asyncio.create_task(self._session(source, i, tgt, q)))
        self.ready.set()
        while not self.stop.is_set():
            await asyncio.sleep(0.2)
        for t in tasks:
            t.cancel()

    async def _preflight(self, mode) -> bool:
        """Vérifie, avant d'ouvrir les sessions, que la clé voit bien le modèle."""
        try:
            await self.client.aio.models.get(model=self.model)
            print(f"✓ Modèle disponible via {mode}.")
            return True
        except Exception as e:
            print(f"   {mode} : modèle non accessible ({str(e)[:160]})")
        try:
            names = [m.name async for m in await self.client.aio.models.list()]
            if any(n.endswith(self.model) for n in names):
                print(f"   …mais il figure dans la liste de {mode} : on tente quand même.")
                return True
            live = [n for n in names if "live" in n or "translate" in n]
            if live:
                print(f"   Modèles « live » visibles via {mode} : " + ", ".join(live))
        except Exception:
            pass
        return False

    async def _session(self, source, idx, tgt, q):
        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "translation_config": {"target_language_code": tgt, "echo_target_language": False},
        }
        name = f"{self.labels[source]} → {tgt}"
        asm = self.assemblers[source]
        backoff = 1
        while not self.stop.is_set():
            try:
                async with self.client.aio.live.connect(model=self.model, config=config) as session:
                    print(f"✓ Connecté à Gemini : {name}")
                    backoff = 1
                    while not q.empty():          # on jette le son accumulé pendant la connexion
                        q.get_nowait()
                    sender = asyncio.create_task(self._send(session, q))
                    receiver = asyncio.create_task(self._receive(session, idx, tgt, asm))
                    done, pending = await asyncio.wait({sender, receiver},
                                                       return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
                    print(f"  ↻ Session {name} terminée par Gemini, reconnexion…")
            except asyncio.CancelledError:
                return
            except Exception as e:
                if self.stop.is_set():
                    return
                if "HTTP 404" in str(e) or "HTTP 403" in str(e) or "HTTP 401" in str(e):
                    print(f"❌ Session {name} refusée par Google : {e}\n"
                          "   Ce n'est pas une coupure passagère : inutile de réessayer.\n"
                          "   Essayez --api-version v1alpha, et voir la section « Erreur HTTP 404 » "
                          "de LISEZMOI.md.")
                    self.stop.set()
                    return
                print(f"  ⚠ Session {name} : {e} — nouvelle tentative dans {backoff} s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _send(self, session, q):
        from google.genai import types
        while True:
            chunk = await q.get()
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={SR}"))

    async def _receive(self, session, idx, tgt, asm):
        while True:
            got = False
            async for msg in session.receive():
                got = True
                if getattr(msg, "go_away", None):
                    return                         # Gemini va fermer : on se reconnecte
                sc = msg.server_content
                if not sc:
                    continue
                # Les deux sessions entendent la même chose : on ne prend le texte
                # original que de la première, pour ne pas l'afficher deux fois.
                if idx == 0 and sc.input_transcription and sc.input_transcription.text:
                    asm.add("in", sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    asm.add("out", sc.output_transcription.text, tgt)
            if not got:
                return                             # connexion fermée


# --------------------------------------------------------------------------
# Envoi vers les sous-titres natifs de Zoom (optionnel)
# --------------------------------------------------------------------------
class ZoomCaptionSender:
    def __init__(self, token_url):
        import requests
        self.requests = requests
        self.url = token_url.strip()
        self.seq = self._last_seq() + 1

    def _last_seq(self):
        try:
            seq_url = self.url.replace("/closedcaption?", "/closedcaption/seq?", 1)
            if seq_url != self.url:
                r = self.requests.get(seq_url, timeout=5)
                if r.ok and r.text.strip().isdigit():
                    return int(r.text.strip())
        except Exception:
            pass
        return -1

    def send(self, text):
        sep = "&" if "?" in self.url else "?"
        r = self.requests.post(f"{self.url}{sep}seq={self.seq}", data=text.encode("utf-8"),
                               headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=5)
        self.seq += 1
        if not r.ok:
            print(f"  ⚠ Zoom a refusé le sous-titre (HTTP {r.status_code}) : {r.text[:120]}")


# --------------------------------------------------------------------------
# Bandeau de sous-titres
# --------------------------------------------------------------------------
def parse_colors(spec: str) -> dict[str, str]:
    """« fr=#FFFFFF,en=#FFE45C » → {"fr": "#FFFFFF", "en": "#FFE45C"}"""
    out = {}
    for part in spec.split(","):
        lang, sep, color = part.partition("=")
        if sep and lang.strip() and color.strip():
            out[lang.strip().lower()] = color.strip()
    return out


def run_overlay(ui_q, args, stop):
    import tkinter as tk

    root = tk.Tk()
    root.title("Sous-titres")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    bg = "#000000"
    root.configure(bg=bg)

    def set_alpha(a):
        try:
            root.attributes("-alpha", a)
        except tk.TclError:
            pass

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    width = int(sw * args.width)
    wrap = width - 60
    frame = tk.Frame(root, bg=bg, padx=30, pady=12)
    frame.pack(fill="both", expand=True)
    big = ("Helvetica", args.font_size, "bold")
    small = ("Helvetica", max(12, int(args.font_size * 0.55)))
    slots = []
    for _ in range(args.lines):
        o = tk.Label(frame, text="", fg="#a0a0a0", bg=bg, font=small, wraplength=wrap, justify="center")
        t = tk.Label(frame, text="", fg="#ffffff", bg=bg, font=big, wraplength=wrap, justify="center")
        slots.append((o, t))

    colors = parse_colors(args.colors)
    lines: dict[int, Caption] = {}
    welcome = Caption(0, "", "", "Sous-titres actifs — parlez pour tester", time.time())
    lines[0] = welcome
    state = {"x": None, "y": None, "sig": None, "drag": (0, 0)}

    def place():
        root.update_idletasks()
        h = frame.winfo_reqheight()
        x = (sw - width) // 2 if state["x"] is None else state["x"]
        y = sh - h - args.margin if state["y"] is None else state["y"]
        root.geometry(f"{width}x{h}+{x}+{y}")

    def render(visible):
        for o, t in slots:
            o.pack_forget()
            t.pack_forget()
        if not visible:
            set_alpha(0.0)                      # bandeau invisible quand personne ne parle
            return
        for (o, t), c in zip(slots, visible):
            if args.show_original and c.original.strip():
                o.config(text=tail(c.original, int(args.max_chars * 1.3)))
                o.pack()
            if c.translation.strip():
                txt = tail(c.translation, args.max_chars)
                t.config(text=f"{c.label} : {txt}" if (args.show_labels and c.label) else txt,
                         fg=colors.get(c.lang.split("-")[0].lower(), "#ffffff"))
                t.pack()
        set_alpha(args.opacity)
        place()

    def poll():
        if stop.is_set():
            root.destroy()
            return
        while True:
            try:
                c = ui_q.get_nowait()
            except queue.Empty:
                break
            lines[c.id] = c
        for k in sorted(lines)[:-20]:           # on oublie les vieilles répliques
            del lines[k]
        now = time.time()
        visible = [c for _, c in sorted(lines.items())
                   if now - c.t < args.ttl and (c.translation.strip() or
                                                (args.show_original and c.original.strip()))]
        visible = visible[-args.lines:]
        sig = tuple((c.id, len(c.original), len(c.translation)) for c in visible)
        if sig != state["sig"]:
            state["sig"] = sig
            render(visible)
        root.after(100, poll)

    def start_drag(e):
        state["drag"] = (e.x_root - root.winfo_x(), e.y_root - root.winfo_y())

    def do_drag(e):
        dx, dy = state["drag"]
        state["x"], state["y"] = e.x_root - dx, e.y_root - dy
        root.geometry(f"+{state['x']}+{state['y']}")

    for w in [root, frame] + [w for pair in slots for w in pair]:
        w.bind("<ButtonPress-1>", start_drag)
        w.bind("<B1-Motion>", do_drag)
        w.bind("<Double-Button-1>", lambda e: stop.set())

    root.after(100, poll)
    root.mainloop()


# --------------------------------------------------------------------------
# Outils : liste des périphériques, test des niveaux et de l'anti-écho
# --------------------------------------------------------------------------
def list_devices():
    import sounddevice as sd
    print("Périphériques d'entrée disponibles :\n")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  ({d['max_input_channels']} canaux, "
                  f"{int(d['default_samplerate'])} Hz)")
    print("\nUtilisez --mic \"<nom ou numéro>\" et --zoom \"<nom ou numéro>\".")


def bar(db, lo=-70, n=24):
    k = int(np.clip((db - lo) / -lo * n, 0, n))
    return "█" * k + " " * (n - k)


def test_audio(args):
    gate = make_gate(args)
    levels = {"mic": -120.0, "zoom": -120.0}
    mic_proc = MicProcessor(gate, lambda x: None)
    caps = []
    if not args.no_zoom:
        def on_zoom(t, x):
            db = rms_db(x)
            levels["zoom"] = db
            if gate:
                gate.update_ref(t, db)
        caps.append(("Zoom ", Capture(args.zoom, on_zoom)))
    if not args.no_mic:
        def on_mic(t, x):
            levels["mic"] = rms_db(x)
            mic_proc.on_block(t, x)
        caps.append(("Micro", Capture(args.mic, on_mic)))
    for label, c in caps:
        print(f"{label} : {c.name} ({c.sr} Hz)")
        c.start()
    t0, zoom_max = time.monotonic(), -120.0
    hint_shown = False
    print("\nFaites parler quelqu'un dans Zoom : le micro doit passer en « COUPÉ (écho) ».")
    print("Parlez vous-même quand personne ne parle dans Zoom : le micro doit rester « ouvert ».")
    print("Ctrl+C pour arrêter.\n")
    try:
        while True:
            state = "COUPÉ (écho)" if mic_proc.gated else "ouvert      "
            zoom_max = max(zoom_max, levels["zoom"])
            if not args.no_zoom and not hint_shown and time.monotonic() - t0 > 15 \
                    and zoom_max < args.zoom_threshold_db:
                hint_shown = True
                print(f"\n\n⚠ Aucun son reçu de Zoom depuis 15 s (max {zoom_max:.0f} dB).\n"
                      "  Vérifiez que le haut-parleur de Zoom est le « Périphérique à sorties\n"
                      "  multiples » et que BlackHole y est coché (voir LISEZMOI.md, Dépannage).\n")
            print(f"\rZoom [{bar(levels['zoom'])}] {levels['zoom']:5.0f} dB   "
                  f"Micro [{bar(levels['mic'])}] {levels['mic']:5.0f} dB   micro : {state}",
                  end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        for _, c in caps:
            c.close()


def make_gate(args):
    if args.no_echo_gate or args.no_zoom or args.no_mic:
        return None
    return EchoGate(ref_threshold_db=args.zoom_threshold_db, hold_s=args.echo_hold,
                    double_talk_db=args.double_talk_db if args.double_talk else None)


# --------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Sous-titres traduits en direct pour Zoom, avec Gemini 3.5 Live Translate.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("audio")
    g.add_argument("--list-devices", action="store_true", help="liste les entrées audio et quitte")
    g.add_argument("--test-audio", action="store_true", help="affiche les niveaux et l'état de l'anti-écho")
    g.add_argument("--mic", default=None, help="micro (nom partiel ou numéro) ; défaut : micro du système")
    g.add_argument("--zoom", default="BlackHole", help="entrée qui reçoit le son de Zoom")
    g.add_argument("--no-mic", action="store_true", help="ne pas écouter le micro")
    g.add_argument("--no-zoom", action="store_true", help="ne pas écouter le son de Zoom")

    g = p.add_argument_group("anti-écho (sans casque)")
    g.add_argument("--zoom-threshold-db", type=float, default=-55,
                   help="niveau du son Zoom à partir duquel on considère que quelqu'un parle")
    g.add_argument("--echo-hold", type=float, default=0.5,
                   help="le micro reste coupé ce temps après la fin du son Zoom (s)")
    g.add_argument("--double-talk", action="store_true",
                   help="laisser passer votre voix si vous parlez par-dessus un participant")
    g.add_argument("--double-talk-db", type=float, default=10,
                   help="marge au-dessus de l'écho pour --double-talk (dB)")
    g.add_argument("--no-echo-gate", action="store_true", help="désactiver l'anti-écho (avec un casque)")

    g = p.add_argument_group("traduction")
    g.add_argument("--langs", default="fr,en",
                   help="langues cibles : chaque phrase est traduite vers celle qu'on ne parle pas")
    g.add_argument("--model", default=DEFAULT_MODEL)
    g.add_argument("--api-version", default=None, help="version de l'API Gemini (ex. v1alpha) ; défaut : celle du SDK")
    g.add_argument("--gap", type=float, default=1.5, help="silence qui termine une réplique (s)")

    g = p.add_argument_group("affichage")
    g.add_argument("--mic-label", default="Hôte", help="nom affiché pour votre micro")
    g.add_argument("--zoom-label", default="Participant", help="nom affiché pour le son de Zoom")
    g.add_argument("--no-labels", dest="show_labels", action="store_false", help="ne pas afficher « Hôte : »…")
    g.add_argument("--show-original", action="store_true",
                   help="afficher aussi, en gris, le texte dans la langue d'origine")
    g.add_argument("--colors", default="fr=#FFFFFF,en=#FFE45C",
                   help="couleur de la traduction selon sa langue")
    g.add_argument("--font-size", type=int, default=30)
    g.add_argument("--lines", type=int, default=2, help="nombre de répliques affichées")
    g.add_argument("--max-chars", type=int, default=170, help="longueur max affichée par réplique")
    g.add_argument("--ttl", type=float, default=8, help="durée d'affichage après la dernière mise à jour (s)")
    g.add_argument("--opacity", type=float, default=0.85)
    g.add_argument("--width", type=float, default=0.9, help="largeur du bandeau (fraction de l'écran)")
    g.add_argument("--margin", type=int, default=70, help="distance au bas de l'écran (px)")
    g.add_argument("--no-overlay", action="store_true", help="pas de bandeau (avec --zoom-token)")

    g = p.add_argument_group("divers")
    g.add_argument("--zoom-token", default=None, help="jeton API de sous-titrage tiers de Zoom (optionnel)")
    g.add_argument("--save", default=None, help="enregistre la transcription dans ce fichier texte")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.list_devices:
        list_devices()
        return
    if args.test_audio:
        test_audio(args)
        return

    langs = tuple(l.strip() for l in args.langs.split(",") if l.strip())
    if not langs:
        raise SystemExit("--langs attend au moins une langue, par exemple fr,en")
    if args.no_mic and args.no_zoom:
        raise SystemExit("Aucune source audio : retirez --no-mic ou --no-zoom.")

    stop = threading.Event()
    ui_q, final_q = queue.Queue(), queue.Queue()
    labels = {"mic": args.mic_label, "zoom": args.zoom_label}
    sources = [s for s, off in (("mic", args.no_mic), ("zoom", args.no_zoom)) if not off]
    assemblers = {s: Assembler(labels[s], ui_q, final_q, args.gap) for s in sources}

    bridge = GeminiBridge(args.model, langs, assemblers, labels, stop, args.api_version)
    bridge.start()
    if stop.is_set():
        return

    zoom = ZoomCaptionSender(args.zoom_token) if args.zoom_token else None
    Finalizer(list(assemblers.values()), final_q, zoom, args.save, stop).start()

    gate = make_gate(args)
    caps = []
    if "zoom" in sources:
        def on_zoom(t, x):
            if gate:
                gate.update_ref(t, rms_db(x))
            bridge.push("zoom", x)
        caps.append(("zoom", Capture(args.zoom, on_zoom)))
    if "mic" in sources:
        mic_proc = MicProcessor(gate, lambda x: bridge.push("mic", x))
        caps.append(("mic", Capture(args.mic, mic_proc.on_block)))
    for s, c in caps:
        print(f"Écoute : {c.name}  →  « {labels[s]} »")
        c.start()
    print(f"Anti-écho : {'actif' if gate else 'désactivé'}. "
          f"Langues cibles : {', '.join(langs)}. Ctrl+C ou double-clic sur le bandeau pour arrêter.\n")

    signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        if args.no_overlay:
            while not stop.is_set():
                time.sleep(0.2)
        else:
            run_overlay(ui_q, args, stop)
    finally:
        stop.set()
        for _, c in caps:
            try:
                c.close()
            except Exception:
                pass
        for a in assemblers.values():
            a.flush()
        time.sleep(0.8)
        print("\nArrêt.")


if __name__ == "__main__":
    main()
