"""Pixiv 解析器，支持插画/漫画/小说链接解析，含动图 GIF 转换与 R18 检测。"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, List

import aiohttp

from ...logger import logger

from .base import BaseVideoParser
from ..utils import SkipParse

ARTWORK_RE = re.compile(r"pixiv\.net/(?:en/)?artworks/(\d+)", re.IGNORECASE)
SHORT_RE = re.compile(r"pixiv\.net/i/(\d+)", re.IGNORECASE)
NOVEL_RE = re.compile(r"pixiv\.net/novel/show\.php\?id=(\d+)", re.IGNORECASE)

IMG_HOST = "i.pximg.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

QUALITY_ORDER = ["original", "large", "medium"]

R18_BADWORDS = [s.lower() for s in ["R-18", "R18", "R-18G", "R18G", "R18+", "R18+G"]]


class PixivParser(BaseVideoParser):

    def __init__(
        self,
        refresh_token: str = "",
        image_proxy_host: str = "i.pixiv.re",
        image_quality: str = "large",
        proxy_url: Optional[str] = None,
    ):
        super().__init__("pixiv")
        self.refresh_token = refresh_token
        self.image_proxy_host = image_proxy_host or "i.pixiv.re"
        self.image_quality = image_quality or "large"
        self.proxy_url = proxy_url
        self._api = None
        self._auth_lock = asyncio.Lock()

    def _get_api(self):
        if self._api is None:
            from pixivpy3 import AppPixivAPI
            if self.proxy_url:
                self._api = AppPixivAPI(proxies={"http": self.proxy_url, "https": self.proxy_url})
            else:
                self._api = AppPixivAPI()
        return self._api

    async def _authenticate(self) -> bool:
        if not self.refresh_token:
            return False
        async with self._auth_lock:
            try:
                api = self._get_api()
                await asyncio.to_thread(api.auth, refresh_token=self.refresh_token)
                return True
            except Exception as e:
                logger.error(f"[pixiv] 认证失败: {type(e).__name__}: {e}")
                return False

    # ── URL 匹配 ──────────────────────────────────────

    def can_parse(self, url: str) -> bool:
        if not url:
            return False
        if "pixiv.net" not in url:
            return False
        if ARTWORK_RE.search(url) or SHORT_RE.search(url) or NOVEL_RE.search(url):
            return True
        return False

    def extract_links(self, text: str) -> List[str]:
        seen: set[str] = set()
        result: List[str] = []
        patterns = [
            r"https?://(?:www\.)?pixiv\.net/(?:en/)?artworks/\d+[^\s<>\"'()]*",
            r"https?://(?:www\.)?pixiv\.net/i/\d+[^\s<>\"'()]*",
            r"https?://(?:www\.)?pixiv\.net/novel/show\.php\?id=\d+[^\s<>\"'()]*",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                url = m.group(0).strip()
                if url not in seen:
                    seen.add(url)
                    result.append(url)
        return result

    # ── R18 检测 ─────────────────────────────────────

    @staticmethod
    def is_r18(illust) -> bool:
        x_restrict = getattr(illust, "x_restrict", 0)
        if isinstance(x_restrict, (int, float)) and x_restrict > 0:
            return True
        tags = getattr(illust, "tags", []) or []
        for t in tags:
            name = (getattr(t, "name", "") or "").lower().strip()
            if name in R18_BADWORDS or any(bad in name for bad in R18_BADWORDS):
                return True
        return False

    # ── 解析 ─────────────────────────────────────────

    async def parse(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> Optional[dict]:
        illust_id = None
        novel_id = None
        m = ARTWORK_RE.search(url) or SHORT_RE.search(url)
        if m:
            illust_id = int(m.group(1))
        else:
            m = NOVEL_RE.search(url)
            if m:
                novel_id = int(m.group(1))

        if not illust_id and not novel_id:
            raise SkipParse(f"[pixiv] 无法从URL提取ID: {url}")

        if not await self._authenticate():
            return {
                "url": url,
                "platform": self.name,
                "parser_name": self.name,
                "title": "Pixiv",
                "author": "",
                "desc": "Pixiv API 认证失败，请检查插件配置中的 refresh_token。",
                "image_urls": [],
                "video_urls": [],
                "image_headers": {},
                "video_headers": {},
                "error": "auth_failed",
            }

        try:
            if illust_id is not None:
                return await self._parse_illust(session, illust_id, url)
            else:
                return await self._parse_novel(novel_id, url)
        except SkipParse:
            raise
        except Exception as e:
            logger.error(f"[pixiv] 解析失败: {type(e).__name__}: {e}")
            return {
                "url": url,
                "platform": self.name,
                "parser_name": self.name,
                "title": "Pixiv 解析失败",
                "author": "",
                "desc": str(e),
                "image_urls": [],
                "video_urls": [],
                "image_headers": {},
                "video_headers": {},
                "error": str(e),
            }

    async def _parse_illust(
        self,
        session: aiohttp.ClientSession,
        illust_id: int,
        url: str,
    ) -> Optional[dict]:
        api = self._get_api()
        try:
            result = await asyncio.to_thread(api.illust_detail, illust_id)
        except Exception as e:
            logger.error(f"[pixiv] illust_detail 调用失败: {type(e).__name__}: {e}")
            raise SkipParse(f"[pixiv] 获取插画详情失败: {e}")

        if not result or not hasattr(result, "illust"):
            raise SkipParse(f"[pixiv] 插画不存在或已被删除: {illust_id}")

        illust = result.illust
        title = getattr(illust, "title", "") or ""
        author = getattr(illust.user, "name", "") if hasattr(illust, "user") else ""
        caption = getattr(illust, "caption", "") or ""
        caption_clean = re.sub(r"<[^>]+>", "", caption).strip()
        page_count = getattr(illust, "page_count", 1)
        illust_type = getattr(illust, "type", "illust")
        is_r18 = self.is_r18(illust)

        tags = getattr(illust, "tags", []) or []
        tag_names = []
        for t in tags:
            name = getattr(t, "name", "") if hasattr(t, "name") else str(t)
            trans = getattr(t, "translated_name", "") if hasattr(t, "translated_name") else ""
            if trans and trans != name:
                tag_names.append(f"#{name}({trans})")
            else:
                tag_names.append(f"#{name}")

        desc_parts = []
        if caption_clean:
            desc_parts.append(caption_clean)
        if tag_names:
            desc_parts.append(" ".join(tag_names))
        desc = "\n\n".join(desc_parts)

        image_headers = {
            "Referer": "https://www.pixiv.net/",
            "User-Agent": UA,
        }

        base_meta = {
            "url": url,
            "platform": self.name,
            "parser_name": self.name,
            "title": title,
            "author": author,
            "desc": desc,
            "video_urls": [],
            "image_headers": image_headers,
            "video_headers": {},
            "is_r18": is_r18,
        }

        if illust_type == "ugoira":
            gif_path = await self._process_ugoira(session, illust, title)
            if gif_path:
                desc_prefix = "[GIF/动图]"
                desc = f"{desc_prefix}\n{desc}" if desc else desc_prefix
                base_meta["desc"] = desc
                base_meta["image_urls"] = [[gif_path]]
            else:
                # 转换失败，回退到静态预览图
                desc_prefix = "[动图转换失败，发送静态预览]"
                desc = f"{desc_prefix}\n{desc}" if desc else desc_prefix
                base_meta["desc"] = desc
                base_meta["image_urls"] = self._build_image_urls(illust, page_count)
            return base_meta

        base_meta["image_urls"] = self._build_image_urls(illust, page_count)
        return base_meta

    def _build_image_urls(self, illust, page_count: int) -> List[List[str]]:
        quality_start = QUALITY_ORDER.index(self.image_quality) if self.image_quality in QUALITY_ORDER else 1
        qualities = QUALITY_ORDER[quality_start:]

        result: List[List[str]] = []
        if page_count > 1 and hasattr(illust, "meta_pages") and illust.meta_pages:
            for page in illust.meta_pages:
                urls = []
                for q in qualities:
                    u = getattr(page.image_urls, q, None)
                    if u:
                        urls.append(self._proxy_url(u))
                if urls:
                    result.append(urls)
        else:
            urls = []
            if page_count == 1 and hasattr(illust, "meta_single_page"):
                orig = getattr(illust.meta_single_page, "original_image_url", None)
                if orig and "original" in qualities:
                    urls.append(self._proxy_url(orig))
            if hasattr(illust, "image_urls"):
                for q in qualities:
                    u = getattr(illust.image_urls, q, None)
                    if u and u not in urls:
                        urls.append(self._proxy_url(u))
            if urls:
                result.append(urls)

        return result

    def _proxy_url(self, url: str) -> str:
        if IMG_HOST in url:
            return url.replace(IMG_HOST, self.image_proxy_host)
        return url

    async def _parse_novel(
        self,
        novel_id: int,
        url: str,
    ) -> Optional[dict]:
        api = self._get_api()
        try:
            result = await asyncio.to_thread(api.novel_detail, novel_id)
        except Exception as e:
            logger.error(f"[pixiv] novel_detail 调用失败: {type(e).__name__}: {e}")
            raise SkipParse(f"[pixiv] 获取小说详情失败: {e}")

        if not result or not hasattr(result, "novel"):
            raise SkipParse(f"[pixiv] 小说不存在或已被删除: {novel_id}")

        novel = result.novel
        title = getattr(novel, "title", "") or ""
        author = getattr(novel.user, "name", "") if hasattr(novel, "user") else ""
        caption = getattr(novel, "caption", "") or ""
        caption_clean = re.sub(r"<[^>]+>", "", caption).strip()
        series_name = ""
        if hasattr(novel, "series") and novel.series:
            series_name = getattr(novel.series, "title", "") or ""

        desc_parts = []
        if series_name:
            desc_parts.append(f"系列: {series_name}")
        if caption_clean:
            desc_parts.append(caption_clean)
        desc = "\n\n".join(desc_parts)

        result_dict = {
            "url": url,
            "platform": self.name,
            "parser_name": self.name,
            "title": title,
            "author": author,
            "desc": desc,
            "image_urls": [],
            "video_urls": [],
            "image_headers": {},
            "video_headers": {},
            "is_r18": False,
        }

        if hasattr(novel, "image_urls") and novel.image_urls:
            cover_url = getattr(novel.image_urls, "large", None) or getattr(novel.image_urls, "medium", None)
            if cover_url:
                result_dict["image_urls"] = [[self._proxy_url(cover_url)]]
                result_dict["image_headers"] = {
                    "Referer": "https://www.pixiv.net/",
                    "User-Agent": UA,
                }

        return result_dict

    # ── 动图 GIF 转换 ────────────────────────────────

    async def _process_ugoira(
        self,
        session: aiohttp.ClientSession,
        illust,
        title: str,
    ) -> Optional[str]:
        """下载 ugoira ZIP 并转换为 GIF，返回本地文件路径。失败返回 None。"""
        api = self._get_api()
        try:
            metadata_result = await asyncio.to_thread(api.ugoira_metadata, illust.id)
        except Exception as e:
            logger.error(f"[pixiv] ugoira_metadata 调用失败: {e}")
            return None

        if not metadata_result or not hasattr(metadata_result, "ugoira_metadata"):
            return None

        meta = metadata_result.ugoira_metadata
        if not hasattr(meta, "zip_urls") or not meta.zip_urls.medium:
            logger.error("[pixiv] ugoira ZIP URL 不存在")
            return None

        zip_url = meta.zip_urls.medium
        zip_url = self._proxy_url(zip_url)

        try:
            timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
            async with session.get(
                zip_url,
                headers={"Referer": "https://www.pixiv.net/", "User-Agent": UA},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[pixiv] ugoira ZIP 下载失败, status={resp.status}")
                    return None
                zip_data = await resp.read()
        except Exception as e:
            logger.error(f"[pixiv] ugoira ZIP 下载异常: {e}")
            return None

        return await self._convert_ugoira_to_gif(zip_data, meta, title, illust.id)

    async def _convert_ugoira_to_gif(
        self,
        zip_data: bytes,
        metadata,
        title: str,
        illust_id: int,
    ) -> Optional[str]:
        """将 ugoira ZIP 数据转换为 GIF 并返回文件路径。"""
        try:
            subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, check=True, timeout=10
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("[pixiv] ffmpeg 不可用，无法转换动图")
            return None

        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix=f"pixiv_ugoira_{illust_id}_")
            zip_path = os.path.join(temp_dir, f"{illust_id}.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_data)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_dir)

            if not hasattr(metadata, "frames") or not metadata.frames:
                return None

            frames_dir = Path(temp_dir)
            frame_entries = []

            for i, frame in enumerate(metadata.frames):
                possible_names = [
                    f"frame_{i:06d}.jpg",
                    f"frame_{i:06d}.png",
                    f"{i:06d}.jpg",
                    f"{i:06d}.png",
                    f"frame_{i}.jpg",
                    f"frame_{i}.png",
                ]
                found = None
                for name in possible_names:
                    p = frames_dir / name
                    if p.exists():
                        found = str(p)
                        break
                if found:
                    delay = getattr(frame, "delay", 100)
                    frame_entries.append(f"file '{found}'\nduration {delay / 1000}")

            if not frame_entries:
                return None

            concat_file = os.path.join(temp_dir, "frames.txt")
            with open(concat_file, "w", encoding="utf-8") as f:
                f.write("\n".join(frame_entries))

            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", title or "untitled")
            output_gif = os.path.join(temp_dir, f"{safe_title}_{illust_id}.gif")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-gifflags", "+transdiff",
                output_gif,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=temp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.error("[pixiv] ffmpeg 转换超时")
                return None

            if proc.returncode != 0:
                err_msg = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
                logger.error(f"[pixiv] ffmpeg 转换失败: {err_msg[:200]}")
                return None

            if not os.path.exists(output_gif) or os.path.getsize(output_gif) == 0:
                logger.error("[pixiv] GIF 文件未生成或为空")
                return None

            # 将 GIF 复制到持久化临时文件（temp_dir 会被清理）
            persistent = tempfile.mktemp(suffix=".gif", prefix=f"pixiv_ugoira_{illust_id}_")
            shutil.copy2(output_gif, persistent)
            file_size = os.path.getsize(persistent)
            logger.info(f"[pixiv] ugoira GIF 转换完成: {persistent}, size={file_size} bytes")
            return os.path.normpath(persistent)

        except Exception as e:
            logger.error(f"[pixiv] ugoira 转换异常: {e}")
            return None
        finally:
            if temp_dir:
                try:
                    await asyncio.to_thread(shutil.rmtree, temp_dir, True)
                except Exception:
                    pass
