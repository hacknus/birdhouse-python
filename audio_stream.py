import numpy as np
import sounddevice as sd
import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

SAMPLE_RATE = 44100
FFT_SIZE = 1024
FPS = 30
BLOCK_SIZE = SAMPLE_RATE // FPS

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

clients = set()
audio_queue = asyncio.Queue(maxsize=2)  # Drop frames if too slow


@app.get("/")
def index():
    return FileResponse("static/index.html")


def audio_thread():
    loop = asyncio.new_event_loop()

    def callback(indata, frames, time_info, status):
        if not clients:
            return

        samples = indata[:, 0]
        fft = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        fft = np.log10(fft + 1e-6)

        # Non-blocking put - drops old frames
        try:
            audio_queue.put_nowait(fft.tolist())
        except asyncio.QueueFull:
            pass

    with sd.InputStream(
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            callback=callback
    ):
        threading.Event().wait()


async def broadcast_worker():
    """Send audio data to all clients concurrently"""
    while True:
        data = await audio_queue.get()

        # Send to all clients concurrently
        await asyncio.gather(
            *[send_to_client(ws, data) for ws in list(clients)],
            return_exceptions=True
        )


async def send_to_client(ws: WebSocket, data):
    """Send data with timeout and error handling"""
    try:
        await asyncio.wait_for(ws.send_json(data), timeout=0.1)
    except (asyncio.TimeoutError, Exception):
        clients.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.on_event("startup")
async def startup():
    # Start audio capture thread
    threading.Thread(target=audio_thread, daemon=True).start()

    # Start broadcast worker
    asyncio.create_task(broadcast_worker())


def run_audiostream():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run_audiostream()
