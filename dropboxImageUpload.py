import os
import requests
import mimetypes
import dropbox
import re
from dotenv import load_dotenv
import shutil
from dropbox.files import WriteMode
from scrape_images import extract_image_links
from checkDirectImageLink import safe_filename_from_url,is_direct_image_url

load_dotenv()
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
# DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

dbx = dropbox.Dropbox(
    app_key=APP_KEY,
    app_secret=APP_SECRET,
    oauth2_refresh_token=REFRESH_TOKEN
)

################################
#handle dropbox links
def process_dropbox_link(dropbox_link: str, dropbox_folder: str = "/PropertyListings"):
    """
    Process a Dropbox shared link (file or folder), extract all images,
    upload them to your Dropbox (using existing upload_to_dropbox),
    and return a list of shared links.
    """
    import os
    import re
    import io
    import zipfile
    import tempfile
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

    def ensure_dl_1(url: str) -> str:
        # Force ?dl=1 to download content (file or folder ZIP)
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["dl"] = ["1"]
        new_query = urlencode({k: v[0] if isinstance(v, list) else v for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))

    def is_image_filename(name: str) -> bool:
        _, ext = os.path.splitext(name.lower())
        return ext in IMAGE_EXTS

    try:
        # Resolve redirects and force download
        resolved = requests.get(dropbox_link, allow_redirects=True, timeout=15).url
        dl_url = ensure_dl_1(resolved)

        # Fetch the content (could be a file OR a ZIP if it's a folder)
        r = requests.get(dl_url, stream=True, timeout=60)
        r.raise_for_status()

        # Peek at headers to decide if it's a ZIP (folder) or a single file
        content_type = r.headers.get("Content-Type", "").lower()
        content_disp = r.headers.get("Content-Disposition", "")

        # Read into memory for quick inspection / zip handling
        content_bytes = io.BytesIO()
        for chunk in r.iter_content(1024 * 64):
            content_bytes.write(chunk)
        content_bytes.seek(0)

        uploaded_links = []

        if "zip" in content_type:
            # Folder share → ZIP of the entire folder
            with zipfile.ZipFile(content_bytes) as zf, tempfile.TemporaryDirectory() as tmpdir:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if not is_image_filename(info.filename):
                        continue
                    # Extract this image to temp, then upload
                    extract_path = os.path.join(tmpdir, os.path.basename(info.filename))
                    with zf.open(info) as src, open(extract_path, "wb") as dst:
                        dst.write(src.read())
                    try:
                        link = upload_to_dropbox(extract_path, dropbox_folder)
                        uploaded_links.append(link)
                    except Exception as up_err:
                        print(f"Upload failed for {info.filename}: {up_err}")

        else:
            # Likely a single file shared link
            # Derive filename from Content-Disposition or URL path
            filename = None
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disp)
            if m:
                filename = m.group(1)
            if not filename:
                # fallback from URL
                path_part = urlparse(resolved).path
                filename = os.path.basename(path_part) or "dropbox_file"

            # If it's an image, upload it
            if is_image_filename(filename) or content_type.startswith("image/"):
                with tempfile.NamedTemporaryFile(delete=False) as tmpf:
                    tmpf.write(content_bytes.read())
                    tmpf.flush()
                    local_path = tmpf.name
                try:
                    link = upload_to_dropbox(local_path, dropbox_folder)
                    uploaded_links.append(link)
                finally:
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
            else:
                print(f"Shared file is not an image (Content-Type: {content_type}, Name: {filename}). Skipping.")

        # Deduplicate links
        return list(dict.fromkeys(uploaded_links))

    except Exception as e:
        print(f"Error processing Dropbox link: {e}")
        return []





##################

def download_file_from_url(url, save_path):

    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(save_path, "wb") as f: # same as f = open(save_path, "wb")
            for chunk in response.iter_content(1024):
                f.write(chunk)
    else:
        raise Exception(f"Failed to download file from {url}, status code: {response.status_code}")


def upload_to_dropbox(local_path, dropbox_folder):
    # Ensure the folder exists
    try:
        dbx.files_create_folder_v2(dropbox_folder, autorename=False)
    except dropbox.exceptions.ApiError as e:
        # Ignore error if folder already exists
        if not (hasattr(e.error, "is_path") and e.error.is_path()):
            raise

    file_name = os.path.basename(local_path)
    dropbox_file_path = f"{dropbox_folder}/{file_name}"

    with open(local_path, "rb") as f:
        dbx.files_upload(f.read(), dropbox_file_path, mode=WriteMode("overwrite"))

    # Always return the folder's shared link
    return create_folder_shared_link(dropbox_folder)


def create_folder_shared_link(dropbox_folder: str):
   
    try:
        shared_link_metadata = dbx.sharing_create_shared_link_with_settings(dropbox_folder)
        return shared_link_metadata.url
    except dropbox.exceptions.ApiError as e:
        # If link already exists
        if isinstance(e.error, dropbox.sharing.CreateSharedLinkWithSettingsError):
            links = dbx.sharing_list_shared_links(path=dropbox_folder).links
            if links:
                return links[0].url
        raise


def get_drive_folder_files(drive_folder_link):

    folder_id = drive_folder_link.split("/")[-1]
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    response = requests.get(url)

    if response.status_code != 200:
        raise Exception(f"Failed to access folder: {response.status_code}")


    pattern = r'data-id="([a-zA-Z0-9_-]{10,})".*?aria-label="([^"]+)"'
    files = [{"id": match[0], "name": match[1]} for match in re.findall(pattern, response.text)]

#     → Creates a list of dictionaries like:
# [
#     {"id": "1A2B3C4D5E", "name": "file1.pdf"},
#     {"id": "6F7G8H9I0J", "name": "file2.jpg"}
# ]

    if not files:
        raise Exception("No files found in the shared Google Drive folder.")

    return files


def upload_drive_folder_to_dropbox(drive_folder_link, dropbox_folder="/DriveUploads"):
    """Download files from a public Google Drive folder and upload to Dropbox."""
    os.makedirs("downloads", exist_ok=True)
    files = get_drive_folder_files(drive_folder_link)

    for file in files:
        file_id = file["id"]
        file_name = file["name"]
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        print(f"Downloading {file_name}...")
        with requests.get(download_url, stream=True) as r:
            if r.status_code != 200:
                print(f"Failed to download {file_name}, skipping...")
                continue

            # Detect MIME type and add extension if missing
            content_type = r.headers.get("Content-Type")
            extension = mimetypes.guess_extension(content_type)
            if extension and not file_name.endswith(extension):
                file_name += extension

            local_path = os.path.join("downloads", file_name)
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)

        # Upload to Dropbox
        with open(local_path, "rb") as f:
            dropbox_path = f"{dropbox_folder}/{file_name}"
            dbx.files_upload(f.read(), dropbox_path, mode=WriteMode("overwrite"))
            print(f"Uploaded {file_name} to Dropbox: {dropbox_path}")

        #delete locally
        os.remove(local_path)
    # Return shared Dropbox folder link
    return create_folder_shared_link(dropbox_folder)

def handle_Link(links, folder = ""):

    shared_links = []
    os.makedirs("downloads", exist_ok=True)

    for link in links:

        #resolve to final link
        response = requests.get(link, allow_redirects=True, timeout=15)
        link = response.url
        # print(f"Resolved link: {link}")
        if "drive.google.com/drive/folders/" in link:
            print(f"Processing Google Drive folder link: {link}")
            try:
                shared_link = upload_drive_folder_to_dropbox(link, f"/PropertyListings/{folder}" if folder else "/PropertyListings")
                shared_links.append(shared_link)
            except Exception as e:
                print(f"Error processing Google Drive link {link}: {e}")

        elif re.search(r"(?:^https?://)?(?:www\.)?(?:dropbox\.com|dl\.dropboxusercontent\.com)/", link):
                # print(f"Processing Dropbox link: {link}")
                try:
                    uploaded = process_dropbox_link(
                        link,
                        f"/PropertyListings/{folder}" if folder else "/PropertyListings"
                    )
                    shared_links.extend(uploaded)
                except Exception as e:
                    print(f"Error processing Dropbox link {link}: {e}")


        elif link.startswith("http"):
            is_img, ct = is_direct_image_url(link)
            if is_img:
                # print(f"Processing direct image link: {link}")
                try:
                    file_name = safe_filename_from_url(link, ct)
                    local_file = os.path.join("downloads", file_name)

                    download_file_from_url(link, local_file)
                    dropbox_path = upload_to_dropbox(
                        local_file,
                        f"/PropertyListings/{folder}" if folder else "/PropertyListings"
                    )

                    if os.path.exists(local_file):
                        os.remove(local_file)

                    shared_links.append(dropbox_path)
                   
                except Exception as e:
                    print(f"Error processing direct link {link}: {e}")
            else:
                # print("Processing web page for image scraping")
                try:
                    img_links = extract_image_links(link)
                    # print(f"\n\n Extracted {len(img_links)} image links from {link}")
                    links.extend(img_links)
                    continue
                except Exception as e:
                    print(f"Error extracting images from {link}: {e}")
                            
        else:
            print(f"Unsupported link format: {link}")
    
    return list(set(shared_links)) 




# links=["https://fcvx-zgpvh.maillist-manage.net/click/19daed7a21ecd4c7/19daed7a21ecabd5"]

# shared_links = handle_Link(links,folder="PropertyListings/prop123")

# print("\nFinal Shared Links:")
# for link in shared_links:
#     print(link,"\n")



