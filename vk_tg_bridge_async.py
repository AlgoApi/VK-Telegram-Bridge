from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from dataclasses import dataclass
import os
import uuid
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
from pyrogram import Client, filters  # type: ignore
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from vkbottle.user import Message, User
from vkbottle_types.objects import UsersUserFull

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("vk-tg-bridge")


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    tg_api_id: int
    tg_api_hash: str
    tg_bot_token: str
    tg_target_chat_id: int

    vk_token: str
    vk_target_peer_id: int | None = None

    download_dir: str = "/tmp/vk_tg_bridge"
    caption_prefix: str = "VK"


class MediaType:
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    VIDEO = "video"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class OutboundMedia:
    kind: str
    url: Optional[str] = None
    filename: Optional[str] = None


class VKMediaExtractor:
    def __init__(self, client: User, access_token: str) -> None:
        self.client = client
        self.access_token = access_token

    @staticmethod
    def _pick_best_photo_url(photo_obj: Any) -> Optional[str]:
        sizes = getattr(photo_obj, "sizes", None) or []
        best_url = None
        best_area = -1
        for size in sizes:
            url = getattr(size, "url", None)
            width = getattr(size, "width", 0) or 0
            height = getattr(size, "height", 0) or 0
            area = width * height
            if url and area >= best_area:
                best_url = url
                best_area = area
        return best_url or getattr(photo_obj, "url", None)

    @staticmethod
    def message_source_link(message: Message) -> str:
        peer_id = getattr(message, "peer_id", None)
        message_id = getattr(message, "id", None) or getattr(
            message, "message_id", None
        )
        if peer_id is None or message_id is None:
            return "https://vk.com/"
        return f"https://vk.com/im?sel={peer_id}&msgid={message_id}"

    def extract_filename_info(self, url: str) -> dict:
        parsed = urlparse(url)

        path = parsed.path

        filename = os.path.basename(path)

        if "." in filename:
            name, extension = filename.rsplit(".", 1)
            extension = "." + extension
        else:
            name = filename
            extension = ""

        return {"filename": filename, "name": name, "extension": extension}

    async def extract(self, message: Message) -> tuple[str, list[OutboundMedia]]:
        text = (getattr(message, "text", None) or "").strip()
        media: list[OutboundMedia] = []
        attachments = getattr(message, "attachments", None) or []

        for attachment in attachments:
            a_type = getattr(attachment, "type", None)
            obj = getattr(attachment, a_type, None) if a_type else None

            if a_type == "photo" and obj is not None:
                url = self._pick_best_photo_url(obj)
                if url:
                    media.append(
                        OutboundMedia(
                            kind=MediaType.PHOTO,
                            url=url,
                            filename=self.extract_filename_info(url).get(
                                "filename", f"{obj.owner_id}_{obj.id}.jpg"
                            ),
                        )
                    )

            elif a_type == "audio_message" and obj is not None:
                url = getattr(obj, "link_ogg", None) or getattr(obj, "link_mp3", None)
                if url:
                    media.append(
                        OutboundMedia(
                            kind=MediaType.VOICE,
                            url=url,
                            filename=self.extract_filename_info(url).get(
                                "filename", f"{obj.owner_id}_{obj.id}.ogg"
                            ),
                        )
                    )

            elif a_type == "doc" and obj is not None:
                url = getattr(obj, "url", None)
                title = getattr(obj, "title", None) or "document"
                if url:
                    media.append(
                        OutboundMedia(kind=MediaType.DOCUMENT, url=url, filename=title)
                    )

            elif a_type == "video" and obj is not None:
                title = getattr(obj, "title", None) or "video"

                owner_id = obj.owner_id
                video_id = obj.id
                access_key = getattr(obj, "access_key", None)
                track_code = getattr(obj, "track_code", None)

                vid_string = f"{owner_id}_{video_id}"
                if access_key:
                    vid_string += f"_{access_key}"

                best_url = None

                try:
                    params = {
                        "owner_id": owner_id,
                        "videos": vid_string,
                        "extended": 1,
                        "v": "5.275",
                        "access_token": self.access_token,
                    }

                    if track_code:
                        params["track_code"] = track_code
                    response = await self.client.api.request("video.get", params)

                    items = response.get("response", response).get("items", [])

                    if items:
                        video_data = items[0]
                        files = video_data.get("files", {})

                        if files:
                            for res in [
                                "mp4_1080",
                                "mp4_720",
                                "mp4_480",
                                "mp4_360",
                                "mp4_240",
                                "mp4_144",
                            ]:
                                if res in files:
                                    best_url = files[res]
                                    break

                        if not best_url and video_data.get("player"):
                            best_url = video_data.get("player")

                except Exception as e:
                    logger.error("Failed to fetch full video via API: %s", e)

                if best_url:
                    media.append(
                        OutboundMedia(
                            kind=MediaType.VIDEO,
                            url=best_url,
                            filename=f"{vid_string}.mp4",
                        )
                    )

        return text, media


class FileDownloader:
    def __init__(self, session: aiohttp.ClientSession, download_dir: Path) -> None:
        self.session = session
        self.download_dir = download_dir

    async def download(self, url: str, filename: str | None = None) -> Path:
        suffix = Path(filename or url.split("?")[0]).suffix
        if not suffix:
            guessed = mimetypes.guess_extension(self._guess_mime(filename or url))
            suffix = guessed or ""

        path = self.download_dir / (filename or f"{uuid.uuid4().hex}_{suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self.session.get(url) as resp:
            resp.raise_for_status()
            with path.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    f.write(chunk)
        return path

    async def delete(self, file_path: Path):
        if file_path.exists() and file_path.is_file():
            file_path.unlink()

    @staticmethod
    def _guess_mime(name: str) -> str:
        mt, _ = mimetypes.guess_type(name)
        return mt or "application/octet-stream"


class TelegramRelay:
    def __init__(self, client: Client, target_chat_id: int) -> None:
        self.client = client
        self.target_chat_id = target_chat_id

    async def send_text(self, text: str) -> None:
        await self.client.send_message(
            self.target_chat_id, text, disable_web_page_preview=True
        )

    async def send_photo(self, path: Path, caption: str | None = None) -> None:
        await self.client.send_photo(
            self.target_chat_id,
            str(path),
            caption=caption or "",
        )

    async def send_voice(self, path: Path, caption: str | None = None) -> None:
        await self.client.send_voice(
            self.target_chat_id,
            str(path),
            caption=caption or "",
        )

    async def send_video(self, path: Path, caption: str | None = None) -> None:
        await self.client.send_video(
            self.target_chat_id,
            str(path),
            caption=caption or "",
        )

    async def send_document(self, path: Path, caption: str | None = None) -> None:
        await self.client.send_document(
            self.target_chat_id,
            str(path),
            caption=caption or "",
        )


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = Client(
            name="sessions/vk_tg_bridge",
            api_id=cfg.tg_api_id,
            api_hash=cfg.tg_api_hash,
            bot_token=cfg.tg_bot_token,
        )
        self.vk = User(cfg.vk_token)
        self.extractor = VKMediaExtractor(self.vk, cfg.vk_token)
        self.session: aiohttp.ClientSession | None = None
        self.downloader: FileDownloader | None = None
        self.relay = TelegramRelay(self.tg, cfg.tg_target_chat_id)
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=None)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.downloader = FileDownloader(self.session, Path(self.cfg.download_dir))

        await self.tg.start()
        logger.info("Telegram client started")

        self._vk_task = asyncio.create_task(self._run_vk())
        logger.info("VK polling task created")

        try:
            await self._shutdown_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if hasattr(self, "_vk_task") and not self._vk_task.done():
            self._vk_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._vk_task

        if self.session and not self.session.closed:
            await self.session.close()

        try:
            await self.tg.stop()
        except Exception:
            logger.exception("Failed to stop Telegram client cleanly")

    async def _run_vk(self) -> None:
        @self.vk.on.chat_message()
        async def on_message(message: Message) -> None:
            if (
                self.cfg.vk_target_peer_id is not None
                and getattr(message, "peer_id", None) != self.cfg.vk_target_peer_id
            ):
                return
            await self.handle_vk_message(message)

        logger.info("Starting manual VK polling loop without LoopWrapper...")

        polling = self.vk.polling
        try:
            async for event in polling.listen():
                logger.debug(f"New event received: {event}")

                updates = event.get("updates", [])
                for update in updates:
                    asyncio.create_task(self.vk.router.route(update, polling.api))
        except Exception as e:
            logger.error(f"Error in VK Polling loop: {e}")

    async def get_author_label(self, message: Message) -> str:
        sender_id = getattr(message, "sender_id", None) or getattr(
            message, "from_id", None
        )

        user: UsersUserFull = await message.get_user()

        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)
        title = getattr(user, "title", None)

        if title:
            return str(title)

        full_name = " ".join(part for part in [first_name, last_name] if part)
        if full_name:
            return full_name

        if sender_id is not None:
            return f"id{sender_id}"

        return "unknown"

    async def handle_vk_message(self, message: Message) -> None:
        assert self.downloader is not None

        text, media = await self.extractor.extract(message)
        source_link = self.extractor.message_source_link(message)
        author_label = await self.get_author_label(message)
        header = f"[{self.cfg.caption_prefix}]({source_link})\nОт: {author_label}"

        if not text and not media:
            await self.relay.send_text(header)
            return

        if media:
            caption = f"{header}\n\n{text}".strip() if text else header
            downloaded_paths: list[tuple[OutboundMedia, Path]] = []
            tg_media_list: list[
                InputMediaPhoto | InputMediaVideo | InputMediaAudio | InputMediaDocument
            ] = []
            try:
                for item in media:
                    try:
                        if item.url:
                            path = await self.downloader.download(
                                item.url, item.filename
                            )
                            current_caption = caption
                            caption = ""
                            path_str = str(path)

                            if item.kind == MediaType.PHOTO:
                                tg_media_list.append(
                                    InputMediaPhoto(path_str, caption=current_caption)
                                )
                            elif item.kind == MediaType.VIDEO:
                                tg_media_list.append(
                                    InputMediaVideo(path_str, caption=current_caption)
                                )
                            elif item.kind == MediaType.VOICE:
                                tg_media_list.append(
                                    InputMediaAudio(path_str, caption=current_caption)
                                )
                            else:
                                tg_media_list.append(
                                    InputMediaDocument(
                                        path_str, caption=current_caption
                                    )
                                )
                    except Exception:
                        logger.exception(f"Failed to download {item.url}")

                if not tg_media_list:
                    await self.relay.send_text(f"{caption}\n\n[Ошибка загрузки медиа]")
                    return

                chunks = [
                    tg_media_list[i : i + 10] for i in range(0, len(tg_media_list), 10)
                ]
                for chunk in chunks:
                    try:
                        await self.tg.send_media_group(
                            self.cfg.tg_target_chat_id, media=chunk
                        )
                    except FloodWait as e:
                        if isinstance(e.value, int):
                            await asyncio.sleep(e.value + 2)
                        else:
                            await asyncio.sleep(60)
                        await self.tg.send_media_group(
                            self.cfg.tg_target_chat_id, media=tg_media_list
                        )
                    except Exception as e:
                        logger.exception(f"Failed to send media group {e}")
                        media_el: (
                            InputMediaPhoto
                            | InputMediaVideo
                            | InputMediaAudio
                            | InputMediaDocument
                        )
                        for media_el in chunk:
                            try:
                                media_el_path: Path
                                if isinstance(media_el.media, Path):
                                    media_el_path = media_el.media
                                elif isinstance(media_el.media, str):
                                    media_el_path = Path(media_el.media)
                                if item.kind == MediaType.PHOTO:
                                    await self.relay.send_photo(
                                        media_el_path, caption=caption
                                    )
                                elif item.kind == MediaType.VOICE:
                                    await self.relay.send_voice(
                                        media_el_path, caption=caption
                                    )
                                elif item.kind == MediaType.VIDEO:
                                    await self.relay.send_video(
                                        media_el_path, caption=caption
                                    )
                                else:
                                    await self.relay.send_document(
                                        media_el_path, caption=caption
                                    )
                            except Exception as e:
                                logger.error(
                                    f"failed fallback try to send {media_el_path} skip..."
                                )
            finally:
                for media_el in tg_media_list:
                    if isinstance(media_el.media, Path):
                        media_el_path = media_el.media
                    elif isinstance(media_el.media, str):
                        media_el_path = Path(media_el.media)
                    await self.downloader.delete(media_el_path)
        else:
            await self.relay.send_text(f"{header}\n\n{text}".strip())


async def main() -> None:
    cfg = Config()  # type: ignore
    bridge = Bridge(cfg)
    await bridge.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
