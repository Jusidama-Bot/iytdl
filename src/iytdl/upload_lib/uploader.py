__all__ = ["Uploader"]

import asyncio
import logging
import os

from shutil import rmtree
from typing import Any, Dict, Literal, Optional, Union

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
    InputMediaAnimation,
    Message,
)
from pathlib import Path

from iytdl.processes import Process
from iytdl.upload_lib import ext
from iytdl.upload_lib.functions import (
    split_video,
    take_screen_shot,
    get_metadata,
    unquote_filename,
    covert_to_jpg,
)  # noqa ignore=F405
from iytdl.upload_lib.progress import progress as upload_progress

from iytdl.utils import *  # noqa ignore=F405


logger = logging.getLogger(__name__)


class Uploader:
    async def find_media(
        self, key: str, media_type: Literal["audio", "video"]
    ) -> Dict[str, Any]:
        """Search Downloaded files for thumbnail and media

        Parameters:
        ----------
            - key (`str`): Unique Key i.e Subfolder name.
            - media_type (`Literal['audio', 'video']`).

        Raises:
        ------
            `TypeError`: In case of Invalid 'media_type'
            `FileNotFoundError`: If Subfolder doesn't exists

        Returns:
        -------
            `Dict[str, Any]`
        """
        if media_type not in ("video", "audio"):
            raise TypeError("'media_type' only accepts video or audio")

        media_path: Path = self.download_path.joinpath(key)
        if not media_path.is_dir():
            raise FileNotFoundError(f"'{media_path}' doesn't exist !")
        info_dict: Dict = {}
        for file in media_path.iterdir():
            if (
                not info_dict.get(media_type)
                and file.name.lower().endswith(getattr(ext, media_type))
                and file.stat().st_size != 0
            ):
                file = unquote_filename(file.absolute())
                if (
                    file.stat().st_size > 2147000000 and media_type == "video"
                ):  # 2 * 1024 * 1024 * 1024 = 2147483648
                    # raise ValueError(f"[{file}] will not be uploaded as filesize exceeds '2 GB', file size is {file.stat().st_size} !")
                    f_path = await split_video(
                        file.absolute(), ffmpeg=self._ffmpeg, ffprobe=self._ffprobe
                    )
                    info_dict["file_name"] = sorted(
                        [os.path.basename(f.absolute()) for f in f_path]
                    )
                    info_dict["is_split"] = True
                else:
                    f_path = file.absolute()
                    info_dict["file_name"] = os.path.basename(f_path)
                    info_dict["is_split"] = False
                info_dict[media_type] = f_path

            if not info_dict.get("thumb") and file.name.lower().endswith(ext.photo):
                info_dict["thumb"], info_dict["size"] = covert_to_jpg(
                    file[0] if isinstance(file, list) else file
                )

            if media_type in info_dict and "thumb" in info_dict:
                break
        logger.debug(f"Debug: {key}\n\n{info_dict}")
        return info_dict

    async def get_input_media(
        self,
        key: str,
        media_type: Literal["audio", "video"],
        caption: str,
        parse_mode: Optional[str] = ParseMode.HTML,
    ) -> Union[InputMediaAudio, InputMediaVideo, None]:
        """Get Input Media

        Parameters:
        ----------
            - key (`str`): Unique Key.
            - media_type (`Literal['audio', 'video']`): audio or video.
            - caption (`str`): Media caption text.
            - parse_mode (`Optional[str]`, optional):
                By default, texts are parsed using both Markdown and HTML styles.
                You can combine both syntaxes together.
                Pass "markdown" or "md" to enable Markdown-style parsing only.
                Pass "ParseMode.HTML" to enable HTML-style parsing only.
                Pass None to completely disable style parsing. (Defaults to `"ParseMode.HTML"`)

        Returns:
        -------
            `Union[InputMediaAudio, InputMediaVideo, None]`
        """
        if media_kwargs := await self.find_media(key, media_type):
            media_kwargs.update(
                {
                    "media": media_kwargs.pop(media_type),
                    "caption": caption,
                    "parse_mode": parse_mode,
                }
            )
            if media_type == "audio":
                return InputMediaAudio(**media_kwargs)
            if media_type == "video":
                return InputMediaVideo(**media_kwargs)

    async def upload(
        self,
        client: Client,
        key: str,
        downtype: str,
        update: Union[CallbackQuery, Message],
        caption_link: Optional[str] = None,
        with_progress: bool = True,
        cb_extra: Union[int, str, None] = None,
    ) -> Union[CallbackQuery, Message]:
        """Upload downloaded Media with progress

        Parameters:
        ----------
            - client (`Client`): Pyrogram Bot Client.
            - key (`str`): Unique key to find downloaded media.
            - downtype (`str`): (`Literal['audio', 'video']`).
            - update (`Union[CallbackQuery, Message]`): Pyrogram Update to edit message.
            - caption_link (`Optional[str]`, optional): Custom caption href link. (Defaults to `None`)
            - with_progress (`bool`, optional): Enable / Disable progress. (Defaults to `True`)
            - cb_extra (`Union[int, str, None]`, optional): Extra callback_data for cancel markup (Defaults to `None`)

        Returns:
        -------
            `Union[CallbackQuery, Message]`: On Success
        """
        if not (mkwargs := await self.find_media(key, downtype)):
            return
        self.msg = update.message if hasattr(update, "message") else update
        process = Process(update, cb_extra=cb_extra)
        try:
            if downtype == "video":
                return await self.__upload_video(
                    client, process, caption_link, mkwargs, downtype, with_progress
                )
            if downtype == "audio":
                return await self.__upload_audio(
                    client,
                    process,
                    caption_link,
                    mkwargs,
                    downtype,
                    with_progress,
                )
        finally:
            if self.delete_file_after_upload:
                rmtree(self.download_path.joinpath(key), ignore_errors=True)

    async def __upload_video(
        self,
        client: Client,
        process: Process,
        caption_link: str,
        mkwargs: Dict[str, Any],
        media_type: str,
        with_progress: bool = True,
    ):

        is_split = mkwargs.pop("is_split")

        if is_split:
            await process.edit("`File is Splitted...`")

            async def send_video(
                c,
                p,
                g_id,
                file,
                file_name,
                caption,
                with_progress,
                total_file=None,
                **mkwargs,
            ):
                m = await c.send_video(
                    chat_id=g_id,
                    video=file,
                    caption=f"📹  {caption}",
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                    progress=upload_progress if with_progress else None,
                    progress_args=(c, p, file_name, "upload", 8, total_file)
                    if with_progress
                    else (),
                    **mkwargs,
                )
                await asyncio.sleep(2)
                return m

            uploaded = []
            videos, videos_name = mkwargs.pop("video"), mkwargs.pop("file_name")
            nums = 1
            size = mkwargs.pop("size", (1280, 720))
            for file, file_name in zip(videos, videos_name):
                caption = (
                    f"<a href={caption_link}>{file_name}</a>"
                    if caption_link
                    else f"<code>{file_name}</code>"
                )
                total_file = {"all_videos": len(videos), "now_video": nums}
                thumb = mkwargs.pop("thumb", None)
                if not thumb:
                    ttl = (
                        (duration // 2) if (duration := mkwargs.get("duration")) else -1
                    )
                    thumb = await take_screen_shot(
                        file,
                        ttl,
                        ffmpeg=self._ffmpeg,
                        ffprobe=getattr(self, "_ffprobe", None),
                    )
                meta = get_metadata(file, media_type, size=size)
                mkwargs.update(meta)
                uploaded.append(
                    await send_video(
                        client,
                        process,
                        self.log_group_id,
                        file,
                        file_name,
                        caption,
                        with_progress,
                        total_file=total_file,
                        thumb=thumb,
                        **mkwargs,
                    )
                )
                nums += 1
        else:
            size = mkwargs.pop("size", (1280, 720))
            meta = get_metadata(mkwargs["video"], media_type, size=size)
            mkwargs.update(meta)
            if not mkwargs.get("thumb"):
                ttl = (duration // 2) if (duration := mkwargs.get("duration")) else -1

                mkwargs["thumb"] = await take_screen_shot(
                    mkwargs["video"],
                    ttl,
                    ffmpeg=self._ffmpeg,
                    ffprobe=getattr(self, "_ffprobe", None),
                )
            caption = (
                f"<a href={caption_link}>{mkwargs['file_name']}</a>"
                if caption_link
                else f"<code>{mkwargs['file_name']}</code>"
            )
            uploaded = await client.send_video(
                chat_id=self.log_group_id,
                caption=f"📹  {caption}",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
                progress=upload_progress if with_progress else None,
                progress_args=(client, process, mkwargs["file_name"])
                if with_progress
                else (),
                **mkwargs,
            )
        if not uploaded:
            return
        await asyncio.sleep(2)

        def __get_inputs(uploaded):
            if uploaded.video:
                return InputMediaVideo(
                    uploaded.video.file_id, caption=uploaded.caption.html
                )
            elif uploaded.animation:
                return InputMediaAnimation(
                    uploaded.animation.file_id, caption=uploaded.caption.html
                )
            elif uploaded.document:
                return InputMediaDocument(
                    uploaded.document.file_id, caption=uploaded.caption.html
                )

        if not process.is_cancelled:
            if not is_split:
                return await process.edit_media(
                    __get_inputs(uploaded), reply_markup=None
                )
            new_caption = "**🗂 Files Splitted Because More Than 2GB**\n\n"
            m = await process.edit(new_caption)
            uploads, child_up = [], []
            for ups in uploaded:
                child_up.append(__get_inputs(ups))
                if len(child_up) == 10:
                    uploads.append(child_up)
                    child_up = []
            if len(child_up) != 0:
                uploads.append(child_up)

            for upload in uploads:
                new_msg, _ = await asyncio.gather(
                    m.reply_media_group(upload, quote=True),
                    asyncio.sleep(2),
                )
                for i, msg in enumerate(new_msg, start=1):
                    _ = msg.video or msg.document
                    new_caption += f"{i}. <a href={msg.link}>{_.file_name}</a>\n"
            return await process.edit(new_caption)

    async def __upload_audio(
        self,
        client: Client,
        process: Process,
        caption_link: str,
        mkwargs: Dict[str, Any],
        media_type: str,
        with_progress: bool = True,
    ):
        mkwargs.pop("is_split")
        caption = (
            f"<a href={caption_link}>{mkwargs['file_name']}</a>"
            if caption_link
            else f"<code>{mkwargs['file_name']}</code>"
        )
        size = mkwargs.pop("size", (1280, 720))
        meta = get_metadata(mkwargs[media_type], media_type, size=size)
        mkwargs |= meta
        uploaded = await client.send_audio(
            chat_id=self.log_group_id,
            caption=f"🎵  {caption}",
            parse_mode=ParseMode.HTML,
            disable_notification=True,
            progress=upload_progress if with_progress else None,
            progress_args=(client, process, mkwargs["file_name"])
            if with_progress
            else (),
            **mkwargs,
        )
        if not uploaded:
            return
        await asyncio.sleep(2)
        if not process.is_cancelled:
            if uploaded.audio:
                return await process.edit_media(
                    media=InputMediaAudio(
                        uploaded.audio.file_id, caption=uploaded.caption.html
                    ),
                    reply_markup=None,
                )
            elif uploaded.document:
                return await process.edit_media(
                    media=InputMediaDocument(
                        uploaded.document.file_id, caption=uploaded.caption.html
                    ),
                    reply_markup=None,
                )
