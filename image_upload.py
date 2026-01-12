import requests
from dotenv import dotenv_values
from pathlib import Path

def upload_image(image_path, token, url):
    """
    Upload image as multipart/form-data with fields:
      - file (file upload)
      - filename (text)
      - auth_token (text)
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"{image_path} not found")

    filename = image_path.name

    with image_path.open("rb") as f:
        files = {"file": (filename, f, "application/octet-stream")}
        data = {"filename": filename, "auth_token": token}

        resp = requests.post(url, files=files, data=data, timeout=30)

    if resp.status_code == 200:
        print("✓ HTTP 200")
        try:
            print(resp.json())
        except Exception:
            print(resp.text)
    else:
        print(f"✗ HTTP {resp.status_code}: {resp.text}")

    return resp

if __name__ == "__main__":
    env = dotenv_values(".env")
    upload_image_token = env.get("UPLOAD_IMAGE_TOKEN")
    upload_image_url = env.get("UPLOAD_IMAGE_URL")

    # Ensure URL ends exactly with /api/upload_image
    if not upload_image_url or not upload_image_url.rstrip("/").endswith("/api/upload_image"):
        raise SystemExit("Set UPLOAD_IMAGE_URL to the server endpoint ending with `/api/upload_image`")

    upload_image("test.png", token=upload_image_token, url=upload_image_url)