"""
Browse pixiv in the terminal using kitty's icat to display images (in the
terminal!)

Requires [kitty](https://github.com/kovidgoyal/kitty) on Linux. It uses the
magical `kitty +kitten icat` 'kitten' to display images.

Capitalized tag definitions:
    TODO: to-do, high priority
    SPEED: speed things up, high priority
    FEATURE: extra feature, low priority
    BLOCKING: this is blocking the prompt but I'm stuck on how to proceed
"""

import os
import re
import sys
import queue
import imghdr
import threading
from configparser import ConfigParser
from concurrent.futures import ThreadPoolExecutor

import pixcat
from blessed import Terminal
from tqdm import tqdm
from pixivpy3 import AppPixivAPI

import pure
from lscat import main as lscat


# - Logging in function
def setup(out_queue):
    """
    Logins to pixiv in the background, using credentials from config file.

    Parameters
    ----------
    out_queue : queue.Queue()
        queue for storing logged-in API object. Needed for threading
    """
    # Read config.ini file
    config_object = ConfigParser()
    config_path = os.path.expanduser("~/.config/koneko/")
    config_object.read(f"{config_path}config.ini")
    config = config_object["Credentials"]

    global API
    API = AppPixivAPI()
    API.login(config["Username"], config["Password"])
    out_queue.put(API)


class LastPageException(ValueError):
    pass


# - Uses web requests, impure
@pure.spinner("Fetching user illustrations... ")
def user_illusts_spinner(artist_user_id):
    # There's a delay here
    # Threading won't do anything meaningful here...
    # SPEED: Caching it (in non volatile storage) might work
    return API.user_illusts(artist_user_id)


@pure.spinner("Getting full image details... ")
def full_img_details(png=False, post_json=None, image_id=None):
    """
    All in one function that gets the full-resolution url, filename, and
    filepath of given image id. Or it can get the id given the post json
    """
    if image_id and not post_json:
        current_image = API.illust_detail(image_id)
        post_json = current_image.illust

    url = pure.change_url_to_full(post_json, png)
    filename = pure.split_backslash_last(url)
    filepath = pure.generate_filepath(filename)
    return url, filename, filepath


# - Download functions
# - Core download functions (for async)
# TODO: consider splitting into rename and download functions
def async_download_core(
    download_path, urls, rename_images=False, file_names=None, pbar=None
):
    """
    Core logic for async downloading. Rename files with given new name
    if needed. Submit each url to the ThreadPoolExecutor.
    """
    oldnames = list(map(pure.split_backslash_last, urls))
    if rename_images:
        newnames = list(
            map(pure.prefix_filename, oldnames, file_names, range(len((urls))))
        )
    else:
        newnames = oldnames

    os.makedirs(download_path, exist_ok=True)
    with pure.cd(download_path):
        with ThreadPoolExecutor(max_workers=30) as executor:
            for (i, name) in enumerate(newnames):
                if not os.path.isfile(name):
                    executor.submit(
                        downloadr, urls[i], oldnames[i], newnames[i], pbar=pbar
                    )


def downloadr(url, img_name, new_file_name=None, pbar=None):
    """Actually downloads one pic given the single url, rename if needed."""
    try:
        # print(f"Downloading {img_name}")
        API.download(url)
    except (RemoteDisconnected, ConnectionError, PixivError) as e:
        # TODO: retry
        print(f"Network error! Caught {e}")

    if pbar:
        pbar.update(1)
    # print(f"{img_name} done!")
    if new_file_name:
        os.rename(img_name, new_file_name)


# - Wrappers around the core functions for async download
@pure.spinner(" Downloading illustrations...  ")
def download_page(current_page_illusts, current_page_num, artist_user_id, pbar=None):
    """
    Download the illustrations on one page of given artist id (using threads),
    rename them based on the post title

    Parameters
    ----------
    current_page_illusts : JsonDict
        JsonDict holding lots of info on all the posts in the current page
    current_page_num : int
        Page as in artist illustration profile pages. Starts from 1
    artist_user_id : int
    """
    urls = pure.medium_urls(current_page_illusts)
    titles = pure.post_titles_in_page(current_page_illusts)
    download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num}/"

    async_download_core(
        download_path, urls, rename_images=True, file_names=titles, pbar=pbar
    )


@pure.spinner("")
def async_download_spinner(download_path, page_urls):
    async_download_core(download_path, page_urls)


# - Wrappers around the core functions for downloading one image
@pure.spinner("")
def download_core(large_dir, url, filename, try_make_dir=True):
    """Downloads one url, intended for single images only"""
    if try_make_dir:
        os.makedirs(large_dir, exist_ok=True)
    if not os.path.isfile(filename):
        print("   Downloading illustration...", flush=True, end="\r")
        with pure.cd(large_dir):
            downloadr(url, filename, None)


def verify_full_download(filepath):
    verified = imghdr.what(filepath)
    if not verified:
        os.remove(filepath)
        return False
    return True


def download_from_image_view(image_id, png=False):
    """
    This downloads an image, checks if it's valid. If not, retry with png.
    """
    url, filename, filepath = full_img_details(image_id=image_id, png=png)
    homepath = os.path.expanduser("~")
    download_core(
        f"{homepath}/Downloads/", url, filename, try_make_dir=False,
    )

    verified = verify_full_download(filepath)
    if not verified:
        download_from_image_view(image_id, png=True)

    print(f"Image downloaded at {filepath}\n")


# - Functions that are wrappers around download functions, making them impure
# @pure.spinner("")  # No message because it conflicts with download_page()
def prefetch_next_page(current_page_num, artist_user_id, all_pages_cache):
    """
    current_page_num : int
        It is the CURRENT page number, before incrementing
    """
    # print("   Prefetching next page...", flush=True, end="\r")
    next_url = all_pages_cache[str(current_page_num)]["next_url"]
    if not next_url:  # this is the last page
        raise LastPageException

    parse_page = API.user_illusts(**API.parse_qs(next_url))
    all_pages_cache[str(current_page_num + 1)] = parse_page
    current_page_illusts = parse_page["illusts"]

    download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num+1}/"
    if not os.path.isdir(download_path):
        pbar = tqdm(total=len(current_page_illusts), smoothing=0)
        download_page(
            current_page_illusts, current_page_num + 1, artist_user_id, pbar=pbar
        )
        pbar.close
    return all_pages_cache


def go_next_image(
    page_urls, img_post_page_num, number_of_pages, downloaded_images, download_path,
):
    """
    Intended to be from image_prompt, for posts with multiple images.
    Downloads next image it not downloaded, open it, download the next image
    in the background

    Parameters
    img_post_page_num : int
        **Starts from 0**. Page number of the multi-image post.
    """
    # IDEAL: image prompt should not be blocked while downloading
    # But I think delaying the prompt is better than waiting for an image
    # to download when you load it

    # First time from gallery; download next image
    if img_post_page_num == 1:
        url = page_urls[img_post_page_num]
        downloaded_images = list(map(pure.split_backslash_last, page_urls[:2]))
        async_download_spinner(download_path, [url])

    display_image_vp(f"{download_path}{downloaded_images[img_post_page_num]}")

    # Downloads the next image
    try:
        next_img_url = page_urls[img_post_page_num + 1]
    except IndexError:
        pass  # Last page
    else:  # No error
        downloaded_images.append(pure.split_backslash_last(next_img_url))
        async_download_spinner(download_path, [next_img_url])
    print(f"Page {img_post_page_num+1}/{number_of_pages}")

    return downloaded_images


# - Non interactive, visible to user functions
def show_artist_illusts(path, renderer="lscat"):
    """
    Use specified renderer to display all images in the given path
    Default is "lscat"; can be "lscat old" or "lsix" (needs to install lsix first)
    """
    if renderer != "lscat":
        lscat_path = os.getcwd()

    with pure.cd(path):
        if renderer == "lscat":
            lscat(path)
        elif renderer == "lscat old":
            os.system(f"{lscat_path}/legacy/lscat")
        elif renderer == "lsix":
            os.system(f"{lscat_path}/legacy/lsix")


def display_image(post_json, artist_user_id, number_prefix, current_page_num):
    """
    Opens image given by the number (medium-res), downloads large-res and
    then display that.

    Parameters
    ----------
    post_json : JsonDict
        description
    number_prefix : int
        The number prefixed in each image
    artist_user_id : int
    current_page_num : int
    """
    if number_prefix < 10:
        search_string = f"0{number_prefix}_"
    else:
        search_string = f"{number_prefix}_"

    # display the already-downloaded medium-res image first, then download and
    # display the large-res
    os.system("clear")
    os.system(
        f"kitty +kitten icat --silent {KONEKODIR}/{artist_user_id}/{current_page_num}/{search_string}*"
    )

    url = pure.url_given_size(post_json, "large")
    filename = pure.split_backslash_last(url)
    large_dir = f"{KONEKODIR}/{artist_user_id}/{current_page_num}/large/"
    download_core(large_dir, url, filename)

    # BLOCKING: imput is blocking, will not display large image until input
    # received

    os.system("clear")
    os.system(
        f"kitty +kitten icat --silent {KONEKODIR}/{artist_user_id}/{current_page_num}/large/{filename}"
    )


def display_image_vp(filepath):
    os.system(f"kitty +kitten icat --silent {filepath}")


# - Interactive functions (frontend)
# - Prompt functions
def begin_prompt(printmessage=True):
    messages = (
        "",
        "Welcome to koneko v0.1\n",
        "Select an action:",
        "1. View artist illustrations",
        "2. Open pixiv post\n",
        "?. Info",
        "q. Quit",
    )
    if printmessage:
        for message in messages:
            print(" " * 20, message)

    pixcat.Image("pics/71471144_p0.png").thumbnail(400).show(align="left", y=0)
    command = input("Enter a command: ")
    return command


def artist_user_id_prompt():
    artist_user_id = input("Enter artist ID or url:\n")
    return artist_user_id

def quit():
    with term.cbreak():
        while True:
            ans = term.inkey()
            if ans == "y" or ans.code == 343:
                sys.exit(0)
            elif ans:
                break

# - Prompt functions with logic
def image_prompt(
    image_id, artist_user_id, current_page=None, current_page_num=1, **kwargs
):
    """
    Image view commands (No need to press enter):
    b -- go back to the gallery
    n -- view next image in post (only for posts with multiple pages)
    p -- view previous image in post (same as above)
    d -- download this image
    o -- open pixiv post in browser
    h -- show this help

    q -- quit (with confirmation)

    """
    """
    current_page and current_page_num is for gallery view -> next page(s) ->
    image prompt -> back
    kwargs are to store info for posts with multiple pages/images
    """
    if kwargs:  # Posts with multiple pages
        page_urls = kwargs["page_urls"]
        img_post_page_num = kwargs["img_post_page_num"]
        number_of_pages = kwargs["number_of_pages"]
        downloaded_images = kwargs["downloaded_images"]
        # Download next if multi

    with term.cbreak():
        while True:
            print("Enter an image view command:")
            image_prompt_command = term.inkey()

            if image_prompt_command == "o":
                link = f"https://www.pixiv.net/artworks/{image_id}"
                os.system(f"xdg-open {link}")
                print(f"Opened {link} in browser")

            elif image_prompt_command == "d":
                download_from_image_view(image_id)

            elif image_prompt_command == "n":
                if not page_urls:
                    print("This is the only page in the post!")
                    continue
                if img_post_page_num + 1 == number_of_pages:
                    print("This is the last image in the post!")
                    continue

                img_post_page_num += 1  # Be careful of 0 index
                downloaded_images = go_next_image(
                    page_urls,
                    img_post_page_num,
                    number_of_pages,
                    downloaded_images,
                    download_path=kwargs["download_path"],
                )

            elif image_prompt_command == "p":
                if not page_urls:
                    print("This is the only page in the post!")

                if img_post_page_num == 0:
                    print("This is the first image in the post!")
                else:
                    download_path = kwargs["download_path"]
                    img_post_page_num -= 1
                    image_filename = downloaded_images[img_post_page_num]
                    display_image_vp(f"{download_path}{image_filename}")
                    print(f"Page {img_post_page_num+1}/{number_of_pages}")

            elif image_prompt_command == "q":
                print("Are you sure you want to exit?")
                quit()

            elif image_prompt_command == "b":
                break  # Leave cbreak()

            elif image_prompt_command == "":
                pass
            elif image_prompt_command == "h":
                print(image_prompt.__doc__)
            elif image_prompt_command:
                print("Invalid command")
                print(image_prompt.__doc__)
            # End if
        # End while
    # End cbreak()

    # image_prompt_command == "b"
    if current_page_num > 1 or current_page:
        all_pages_cache = kwargs["all_pages_cache"]
        show_gallery(
            artist_user_id,
            current_page_num,
            current_page,
            all_pages_cache=all_pages_cache,
        )
    else:
        # Came from view post mode, don't know current page num
        # Defaults to page 1
        artist_illusts_mode(artist_user_id)


def download_from_gallery(gallery_command, current_page_illusts, png=False):
    number = pure.process_coords_slice(gallery_command)
    if not number:
        number = int(gallery_command[1:])

    post_json = current_page_illusts[number]
    url, filename, filepath = full_img_details(post_json=post_json, png=png)

    homepath = os.path.expanduser("~")
    download_path = f"{homepath}/Downloads/"
    download_core(download_path, url, filename, try_make_dir=False)

    verified = verify_full_download(filepath)
    if not verified:
        download_from_gallery(gallery_command, current_page_illusts, png=True)

    print(f"Image downloaded at {filepath}\n")


def open_link(gallery_command, current_page_illusts):
    number = pure.process_coords_slice(gallery_command)
    if not number:
        number = int(gallery_command[1:])

    image_id = current_page_illusts[number]["id"]
    link = f"https://www.pixiv.net/artworks/{image_id}"
    os.system(f"xdg-open {link}")
    print(f"Opened {link}!\n")


def gallery_prompt(
    current_page_illusts,
    current_page,
    current_page_num,
    artist_user_id,
    all_pages_cache,
):
    """
    Gallery commands: (No need to press enter)
    Using coordinates, where {digit1} is the row and {digit2} is the column
    {digit1}{digit2}   -- display the image on row digit1 and column digit2
    o{digit1}{digit2}  -- open pixiv image/post in browser
    d{digit1}{digit2}  -- download image in large resolution

    Using image number, where {number} is the nth image in order (see examples)
    i{number}          -- display the image
    O{number}          -- open pixiv image/post in browser.
    D{number}          -- download image in large resolution.

    n                  -- view the next page
    p                  -- view the previous page
    h                  -- show this help
    q                  -- exit

    Examples:
        i09   --->  Display the ninth image in image view (must have leading 0)
        i10   --->  Display the tenth image in image view
        O9    --->  Open the ninth image's post in browser
        D9    --->  Download the ninth image, in large resolution

        25    --->  Display the image on column 2, row 5 (index starts at 1)
        d25    --->  Open the image on column 2, row 5 (index starts at 1) in browser
        o25    --->  Download the image on column 2, row 5 (index starts at 1)

    """
    """
    Sequence means a combination of more than one key.
    When a sequenceable key is pressed, wait for the next keys in the sequence
        If the sequence is valid, execute their corresponding actions
    Otherwise for keys that do not need a sequence, execute their actions normally
    """
    # Fixes: Gallery -> next page -> image prompt -> back -> prev page
    # Gallery -> Image -> back still retains all_pages_cache, no need to
    # prefetch again
    if len(all_pages_cache) == 1:
        # Prefetch the next page on first gallery load
        try:
            all_pages_cache = prefetch_next_page(
                current_page_num, artist_user_id, all_pages_cache
            )
        except LastPageException:
            pass
    else:  # Gallery -> next -> image prompt -> back
        all_pages_cache[str(current_page_num)] = current_page

    print(f"Page {current_page_num}")

    sequenceable_keys = ("o", "d", "i")
    with term.cbreak():
        keyseqs = []
        seq_num = 0
        while True:
            print("Enter a gallery command:")
            gallery_command = term.inkey()

            # Wait for the rest of the sequence
            if gallery_command in sequenceable_keys:
                keyseqs.append(gallery_command)
                print(keyseqs)
                seq_num += 1

            # Digits continue the sequence
            elif gallery_command.isdigit():
                keyseqs.append(gallery_command)
                print(keyseqs)

                # End of the sequence...
                # Two digit sequence -- view image given coords
                if seq_num == 1 and keyseqs[0].isdigit() and keyseqs[1].isdigit():

                    first_num = int(keyseqs[0])
                    second_num = int(keyseqs[1])
                    selected_image_num = pure.find_number_map(first_num, second_num)

                    (
                        image_id,
                        current_page,
                        page_urls,
                        number_of_pages,
                        all_pages_cache,
                    ) = gallery_to_image(
                        selected_image_num,
                        current_page_num,
                        artist_user_id,
                        all_pages_cache,
                    )
                    break  # leave cbreak()

                # One letter two digit sequence
                if seq_num == 2 and keyseqs[1].isdigit() and keyseqs[2].isdigit():

                    first_num = keyseqs[1]
                    second_num = keyseqs[2]

                    # Open or download given coords
                    if keyseqs[0] == "o":
                        open_link(f"o{first_num}{second_num}", current_page_illusts)

                    elif keyseqs[0] == "d":
                        download_from_gallery(
                            f"d{first_num}{second_num}", current_page_illusts
                        )

                    # View image, open, or download given image number
                    selected_image_num = int(f"{first_num}{second_num}")

                    if keyseqs[0] == "O":
                        open_link(selected_image_num, current_page_illusts)
                    elif keyseqs[0] == "D":
                        download_from_gallery(
                            selected_image_num, current_page_illusts
                        )
                    elif keyseqs[0] == "i":
                        (
                            image_id,
                            current_page,
                            page_urls,
                            number_of_pages,
                            all_pages_cache,
                        ) = gallery_to_image(
                            selected_image_num,
                            current_page_num,
                            artist_user_id,
                            all_pages_cache,
                        )
                        break

                    # Reset sequence info after running everything
                    keyseqs = []
                    seq_num = 0

                # Not the end of the sequence yet, continue while block
                else:
                    seq_num += 1

            # No sequence, execute their functions immediately
            elif gallery_command == "n":
                download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num+1}/"
                try:
                    show_artist_illusts(download_path)
                except FileNotFoundError:
                    print("This is the last page!")
                    continue
                current_page_num += 1  # Only increment if successful
                print(f"Page {current_page_num}")

                # Skip prefetching again for cases like next -> prev -> next
                if str(current_page_num + 1) not in all_pages_cache.keys():
                    try:
                        # After showing gallery, pre-fetch the next page
                        all_pages_cache = prefetch_next_page(
                            current_page_num, artist_user_id, all_pages_cache
                        )
                    except LastPageException:
                        print("This is the last page!")

            elif gallery_command == "p":
                if current_page_num > 1:
                    # It's -2 because current_page_num starts at 1
                    current_page = all_pages_cache[str(current_page_num - 1)]
                    current_page_illusts = current_page["illusts"]
                    current_page_num -= 1
                    # download_path should already be set
                    download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num}/"
                    show_artist_illusts(download_path)
                    print(f"Page {current_page_num}")

                else:
                    print("This is the first page!")

            elif gallery_command == "q":
                print("Are you sure you want to exit?")
                quit()

            elif gallery_command == "h":
                print(gallery_prompt.__doc__)

            elif gallery_command:
                print("Invalid command")
                print(gallery_prompt.__doc__)
            # End if
        # End while
    # End cbreak()

    # Display image (using either coords or image number), the show this prompt
    image_prompt(
        image_id,
        artist_user_id,
        current_page_num=current_page_num,
        current_page=current_page,
        page_urls=page_urls,
        img_post_page_num=0,
        number_of_pages=number_of_pages,
        downloaded_images=None,
        all_pages_cache=all_pages_cache,
        download_path=f"{KONEKODIR}/{artist_user_id}/{current_page_num}/large/",
    )


# - End interactive (frontend) functions


def gallery_to_image(
    selected_image_num, current_page_num, artist_user_id, all_pages_cache
):
    """
    Do-all function that gets all the info needed to display image, and return
    values needed to pass into image_prompt()
    """
    current_page = all_pages_cache[str(current_page_num)]
    current_page_illusts = current_page["illusts"]
    post_json = current_page_illusts[selected_image_num]
    image_id = post_json.id

    display_image(post_json, artist_user_id, selected_image_num, current_page_num)

    # blocking: no way to unblock prompt
    number_of_pages, page_urls = pure.page_urls_in_post(post_json, "large")

    return image_id, current_page, page_urls, number_of_pages, all_pages_cache


# - Mode and loop functions (some interactive and some not)
def show_gallery(
    artist_user_id, current_page_num, current_page, show=True, all_pages_cache=None
):
    """
    Downloads images, show if requested, instantiate all_pages_cache, prompt.
    """
    download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num}/"
    current_page_illusts = current_page["illusts"]

    if not os.path.isdir(download_path):
        pbar = tqdm(total=len(current_page_illusts), smoothing=0)
        download_page(current_page_illusts, current_page_num, artist_user_id, pbar=pbar)
        pbar.close()

    if show:
        show_artist_illusts(download_path)
    pure.print_multiple_imgs(current_page_illusts)

    if not all_pages_cache:
        all_pages_cache = {"1": current_page}

    gallery_prompt(
        current_page_illusts,
        current_page,
        current_page_num,
        artist_user_id,
        all_pages_cache,
    )


def artist_illusts_mode(artist_user_id, current_page_num=1):
    """
    If artist_user_id dir exists, show immediately (without checking
    for contents!)
    Else, fetch current_page json and proceed download -> show -> prompt
    """
    download_path = f"{KONEKODIR}/{artist_user_id}/{current_page_num}/"
    # If path exists, show immediately (without checking for contents!)
    if os.path.isdir(download_path):
        show_artist_illusts(download_path)
        show = False
    else:
        show = True

    current_page = user_illusts_spinner(artist_user_id)
    show_gallery(artist_user_id, current_page_num, current_page, show=show)


def view_post_mode(image_id):
    """
    Fetch all the illust info, download it in the correct directory, then display it.
    If it is a multi-image post, download the next image
    Else or otherwise, open image prompt
    """
    print("Fetching illust details...")
    try:
        post_json = API.illust_detail(image_id)["illust"]
    except KeyError:
        print("Work has been deleted or the ID does not exist!")
        sys.exit(1)

    url = pure.url_given_size(post_json, "large")
    filename = pure.split_backslash_last(url)
    artist_user_id = post_json["user"]["id"]

    # If it's a multi-image post, download the first pic in individual/{image_id}
    # So it won't be duplicated later
    number_of_pages, page_urls = pure.page_urls_in_post(post_json, "large")
    if number_of_pages == 1:
        large_dir = f"{KONEKODIR}/{artist_user_id}/individual/"
        downloaded_images = None
    else:
        large_dir = f"{KONEKODIR}/{artist_user_id}/individual/{image_id}/"

    download_core(large_dir, url, filename)
    display_image_vp(f"{large_dir}{filename}")

    # Download the next page for multi-image posts
    if number_of_pages != 1:
        async_download_spinner(large_dir, page_urls[:2])
        downloaded_images = list(map(pure.split_backslash_last, page_urls[:2]))

    image_prompt(
        image_id,
        artist_user_id,
        page_urls=page_urls,
        img_post_page_num=0,
        number_of_pages=number_of_pages,
        downloaded_images=downloaded_images,
        download_path=large_dir,
    )
    # Will only be used for multi-image posts, so it's safe to use large_dir
    # Without checking for number_of_pages
    # artist_illusts_mode(artist_user_id)


@pure.catch_ctrl_c
def artist_illusts_mode_loop(prompted, artist_user_id=None):
    """
    Ask for artist ID and process it, wait for API to finish logging in
    before proceeding
    """
    while True:
        if prompted and not artist_user_id:
            artist_user_id = artist_user_id_prompt()
            os.system("clear")
            if "pixiv" in artist_user_id:
                artist_user_id = pure.split_backslash_last(artist_user_id)
            # After the if, input must either be int or invalid
            try:
                int(artist_user_id)
            except ValueError:
                print("Invalid user ID!")
                break

        API_THREAD.join()  # Wait for API to finish
        global API
        API = API_QUEUE.get()  # Assign API to PixivAPI object

        artist_illusts_mode(artist_user_id)


@pure.catch_ctrl_c
def view_post_mode_loop(prompted, image_id=None):
    """
    Ask for post ID and process it, wait for API to finish logging in
    before proceeding
    """
    while True:
        if prompted and not image_id:
            url_or_id = input("Enter pixiv post url or ID:\n")
            os.system("clear")

            # Need to process complex url first
            if "illust_id" in url_or_id:
                image_id = re.findall(r"&illust_id.*", url_or_id)[0].split("=")[-1]
            elif "pixiv" in url_or_id:
                image_id = pure.split_backslash_last(url_or_id)
            else:
                image_id = url_or_id

            # After the if, input must either be int or invalid
            try:
                int(image_id)
            except ValueError:
                print("Invalid image ID!")
                break

        API_THREAD.join()  # Wait for API to finish
        global API
        API = API_QUEUE.get()  # Assign API to PixivAPI object

        view_post_mode(image_id)


@pure.catch_ctrl_c
def info_screen_loop():
    os.system("clear")
    messages = (
        "",
        "koneko こねこ version 0.1 beta\n",
        "Browse pixiv in the terminal using kitty's icat to display images",
        "with images embedded in the terminal\n",
        "View a gallery of an artist's illustrations with mode 1",
        "View a post with mode 2. Posts support one or multiple images\n",
        "Thank you for using koneko!",
        "Please star, report bugs and contribute in:",
        "https://github.com/twenty5151/koneko",
        "GPLv3 licensed\n",
        "Credits to amasyrup (甘城なつき):",
        "Welcome image: https://www.pixiv.net/en/artworks/71471144",
        "Current image: https://www.pixiv.net/en/artworks/79494300",
    )

    for message in messages:
        print(" " * 23, message)

    pixcat.Image("pics/79494300_p0.png").thumbnail(650).show(align="left", y=0)

    while True:
        help_command = input("\n\nPress any key to return: ")
        if help_command or help_command == "":
            os.system("clear")
            break


def main_loop(prompted, main_command=None, artist_user_id=None, image_id=None):
    """Ask for mode selection"""
    # SPEED: gallery mode - if tmp has artist id and '1' dir,
    # immediately show it without trying to log in or download
    printmessage = True
    while True:
        if prompted:
            main_command = begin_prompt(printmessage)

        if main_command == "1":
            artist_illusts_mode_loop(prompted, artist_user_id)

        elif main_command == "2":
            view_post_mode_loop(prompted, image_id)

        elif main_command == "?":
            info_screen_loop()

        elif main_command == "q":
            answer = input("Are you sure you want to exit? [y/N]:\n")
            if answer == "y" or not answer:
                break
            else:
                printmessage = False
                continue

        else:
            print("\nInvalid command!")
            printmessage = False
            continue


def main():
    # It'll never be changed after logging in
    global API, API_QUEUE, API_THREAD
    API_QUEUE = queue.Queue()
    API_THREAD = threading.Thread(target=setup, args=(API_QUEUE,))
    API_THREAD.start()  # Start logging in

    # During this part, the API can still be logging in but we can proceed
    os.system("clear")
    if len(sys.argv) == 2:
        print("Logging in...")

    artist_user_id, image_id = None, None
    # Direct command line arguments, skip begin_prompt()
    if len(sys.argv) == 2:
        prompted = False
        url = sys.argv[1]

        if "users" in url:
            if "\\" in url:
                artist_user_id = pure.split_backslash_last(url).split("\\")[-1][1:]
            else:
                artist_user_id = pure.split_backslash_last(url)
            main_command = "1"

        elif "artworks" in url:
            image_id = pure.split_backslash_last(url).split("\\")[0]
            main_command = "2"

        elif "illust_id" in url:
            image_id = re.findall(r"&illust_id.*", url)[0].split("=")[-1]
            image_id = int(image_id)
            main_command = "2"

    elif len(sys.argv) > 3:
        print("Too many arguments!")
        sys.exit(1)

    else:
        prompted = True
        main_command = None

    try:
        main_loop(prompted, main_command, artist_user_id, image_id)
    except KeyboardInterrupt:
        print("\n")
        answer = input("Are you sure you want to exit? [y/N]:\n")
        if answer == "y" or not answer:
            sys.exit(1)


if __name__ == "__main__":
    global term
    term = Terminal()
    global KONEKODIR
    KONEKODIR = "/tmp/koneko"
    main()
