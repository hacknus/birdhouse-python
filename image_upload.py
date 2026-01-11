import requests


def upload_image(image_path, token="your-token-here",
                 url="http://localhost:8080/api/upload_image1985909069561561095"):
    """Upload an image using Dioxus server function"""

    with open(image_path, 'rb') as f:
        file_data = list(f.read())  # Convert bytes to list of integers
        filename = image_path.split('/')[-1]

        # Dioxus server functions expect arguments as a JSON array
        payload = [file_data, filename, token]

        headers = {
            'Content-Type': 'application/json',
        }

        response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200:
        try:
            result = response.json()
            if result.get("success"):
                print(f"✓ Success: {result['message']}")
            else:
                print(f"✗ Error: {result['message']}")
        except:
            print(f"✓ Response: {response.text}")
    else:
        print(f"✗ HTTP Error {response.status_code}: {response.text}")

    return response


# Example usage
if __name__ == "__main__":
    upload_image("test.png")
