import numpy as np
import sounddevice as sd
import asyncio
import threading
import time
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

SAMPLE_RATE = 44100
FFT_SIZE = 1024
FPS = 30
BLOCK_SIZE = SAMPLE_RATE // FPS

last_send = 0
SEND_INTERVAL = 1 / FPS

app = FastAPI()

# Serve static assets safely
app.mount("/static", StaticFiles(directory="static"), name="static")

clients = set()


@app.get("/")
def index():
    return FileResponse("static/index.html")


def audio_thread(loop):
    def callback(indata, frames, time_info, status):
        global last_send
        now = time.time()
        if now - last_send < SEND_INTERVAL:
            return
        last_send = now
        if not clients:
            return

        samples = indata[:, 0]
        fft = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        fft = np.log10(fft + 1e-6)

        asyncio.run_coroutine_threadsafe(
            broadcast(fft.tolist()), loop
        )

    stop_event = threading.Event()

    with sd.InputStream(
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            callback=callback
    ):
        stop_event.wait()  # ← blocks without CPU usage


async def broadcast(data):
    dead = set()
    for ws in clients:
        try:
            await ws.send_json(data)
        except:
            dead.add(ws)
    clients.difference_update(dead)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except:
        clients.remove(ws)


@app.on_event("startup")
async def startup():
    loop = asyncio.get_running_loop()
    threading.Thread(
        target=audio_thread,
        args=(loop,),
        daemon=True
    ).start()


def run_audiostream():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run_audiostream()
