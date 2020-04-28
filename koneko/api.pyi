import queue
import threading
import configparser
from typing import *

import funcy
from pixivpy3 import AppPixivAPI

class APIHandler:
    API_QUEUE: queue.Queue = ...
    API_THREAD: threading.Thread = ...
    API: AppPixivAPI
    def __init__(self) -> None: ...
    def add_credentials(self, credentials: configparser.SectionProxy) -> None: ...
    def start(self) -> None: ...
    def await_login(self) -> None: ...
    def parse_next(self, next_url: Dict[str, str]): ...
    def artist_gallery_parse_next(self, **kwargs: Any): ...
    def artist_gallery_request(self, artist_user_id: int): ...
    def protected_illust_detail(self, image_id: int): ...
    def search_user_request(self, searchstr: str, offset: str): ...
    def following_user_request(self, user_id: int, publicity: str, offset: str): ...
    def illust_follow_request(self, **kwargs: Any): ...
    def protected_download(self, url: str) -> None: ...
