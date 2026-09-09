"""Self-check for HTML image extraction. Run: python -m media.test_scrape_images"""

from media.check_direct_link import guess_media_extension
from media.scrape_images import (
    drive_folder_id,
    drive_folder_image_urls,
    drive_folder_page_url,
    extract_google_photos_links,
    extract_image_links,
    extract_image_links_from_html,
    gallery_image_urls,
    gallery_url_from_text,
    http_urls,
    page_urls_from_text,
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

    wa_text = (
        "Photo link:\n"
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u?usp=sharing"
    )
    drive = gallery_url_from_text(wa_text)
    assert drive.startswith("https://drive.google.com/drive/folders/"), drive
    assert drive_folder_id(drive) == "1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u", drive_folder_id(drive)
    assert drive_folder_page_url(
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u"
        "?usp=sharing&resourcekey=0-abc"
    ) == (
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u"
        "?resourcekey=0-abc"
    )
    assert http_urls("*https://drive.google.com/drive/folders/abc123XYZ0*") == [
        "https://drive.google.com/drive/folders/abc123XYZ0"
    ]
    assert gallery_url_from_text("https://example.com/listing") == "https://example.com/listing"
    mixed = page_urls_from_text(
        "https://seller.example/unsubscribe\n"
        "https://example.com/listing\n"
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u?usp=sharing"
    )
    assert mixed[0].startswith("https://drive.google.com/drive/folders/"), mixed
    assert "https://example.com/listing" in mixed
    assert all("unsubscribe" not in u for u in mixed)

    drive_html = (
        '<div data-id="abc123XYZ0" aria-label="living.jpg"></div>'
        '<div data-id="folder99999" aria-label="Shared folder"></div>'
        '<div data-id="notes00000" aria-label="notes.pdf"></div>'
    )
    drive_imgs = drive_folder_image_urls(
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u?usp=sharing",
        html=drive_html,
    )
    assert drive_imgs == ["https://drive.google.com/uc?export=download&id=abc123XYZ0"], drive_imgs
    assert gallery_image_urls(
        "https://drive.google.com/drive/folders/1Y7cYtCxDfvZvC06jwOzcjtYPVr7rbU5u?usp=sharing",
        html=drive_html,
    ) == drive_imgs
    print("ok")


if __name__ == "__main__":
    main()
