"""Self-check for HTML image extraction. Run: python -m media.test_scrape_images"""

from media.check_direct_link import guess_media_extension
from media.scrape_images import (
    extract_google_photos_links,
    extract_image_links,
    extract_image_links_from_html,
)

HTML = """
<html>
  <img src="https://track.example/pixel.gif" width="1" height="1"
       data-src="https://cdn.example/photo.jpg">
  <img srcset="https://cdn.example/a.webp 640w, https://cdn.example/b.webp 1280w">
  <img src="data:image/gif;base64,xxxx">
  <img src="https://facebook.com/icon.png">
  <meta property="og:image" content="/og.jpg">
</html>
"""


def main() -> None:
    links = extract_image_links_from_html(HTML, "https://seller.example/album")
    assert "https://cdn.example/photo.jpg" in links, links
    assert "https://cdn.example/a.webp" in links, links
    assert "https://cdn.example/b.webp" in links, links
    assert "https://seller.example/og.jpg" in links, links
    assert "https://track.example/pixel.gif" not in links, links
    assert all("facebook.com" not in u for u in links), links
    assert all(not u.startswith("data:") for u in links), links
    # Passing HTML must not fetch or launch Chromium.
    reused = extract_image_links("https://seller.example/album", html=HTML)
    assert reused == links, reused
    assert guess_media_extension("image/jpeg") == ".jpg"
    assert guess_media_extension("image/jpeg; charset=binary") == ".jpg"

    ghtml = """
    <html>
      <img src="https://lh3.googleusercontent.com/pw/AP1GczPhotoOne=w54-h72-no">
      <img src="https://lh3.googleusercontent.com/a/ACg8ocAvatar=s10-p-no">
      <script>
        const x = "https:\\/\\/lh3.googleusercontent.com\\/pw\\/AP1GczPhotoTwo=w54-h72-no";
        const y = "https://lh3.googleusercontent.com/pw/AP1GczPhotoOne=w600-h315-p-k";
      </script>
    </html>
    """
    gphotos = extract_google_photos_links(ghtml)
    assert gphotos == [
        "https://lh3.googleusercontent.com/pw/AP1GczPhotoOne=w2048",
        "https://lh3.googleusercontent.com/pw/AP1GczPhotoTwo=w2048",
    ], gphotos
    from_album = extract_image_links_from_html(ghtml, "https://photos.app.goo.gl/abc")
    assert from_album == gphotos, from_album
    print("ok")


if __name__ == "__main__":
    main()
