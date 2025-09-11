import requests
from bs4 import BeautifulSoup

def extract_image_links(url: str) -> list:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        valid_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")  # allowed types
        image_links = []

        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue

            full_url = requests.compat.urljoin(url, src)

            # only add if matches allowed extensions (skip .svg etc.)
            if full_url.lower().endswith(valid_exts):
                image_links.append(full_url)

        return image_links

    except Exception as e:
        print(f"Error: {e}")
        return []

# Example usage
