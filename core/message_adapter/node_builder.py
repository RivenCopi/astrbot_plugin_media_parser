"""消息节点构建器，将解析结果转换为可发送消息节点。"""
import os
import subprocess
from typing import Dict, Any, List, Optional, Union

from ..logger import logger

from astrbot.api.message_components import Plain, Image, Video

from ..downloader.utils import strip_media_prefixes
from ..types import BuildAllNodesResult, LinkBuildMeta


_GIF_MAX_SIZE_MB = 20.0  # 单张 GIF 允许的最大字节数

# ========== MP4→GIF 转码常量 ==========
_GIF_FPS = 10           # GIF 帧率
_GIF_MAX_WIDTH = 320    # GIF 最大宽度，超出则等比缩放
_GIF_FLAGS_QUALITY = (
    "fps={fps},scale={w}:-1:flags=lanczos,split[s0][s1];"
    "[s0]palettegen=max_colors=128:stats_mode=diff[s0];"
    "[s1][s0]paletteuse=dither=bayer:bayer_scale=2"
)


def _convert_mp4_to_gif(
    mp4_path: str,
    gif_path: str,
    fps: int = _GIF_FPS,
    max_size_mb: float = _GIF_MAX_SIZE_MB,
) -> bool:
    """用 ffmpeg 将 mp4 转码为 GIF 动图。

    使用双通道调色板（palettegen + paletteuse）保证画质，
    同时通过 FPS/分辨率限制控制输出文件大小。若目标 GIF
    生成后仍超过 ``max_size_mb``，则自动降档重试
    （先降 FPS，再降分辨率），最终仍不满足则返回 False。

    Args:
        mp4_path: 源 mp4 路径
        gif_path: 目标 gif 路径
        fps: 帧率（默认 10）
        max_size_mb: 最大文件大小 (MB)，超过则降档重试
    """
    import re as _re

    fps = int(fps)
    max_size_mb = float(max_size_mb)
    max_width = _GIF_MAX_WIDTH
    passed = False

    for attempt in range(3):
        vf = _GIF_FLAGS_QUALITY.format(fps=fps, w=max_width)
        cmd = [
            "ffmpeg", "-y",
            "-i", mp4_path,
            "-vf", vf,
            "-loop", "0",
            gif_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            size_mb = os.path.getsize(gif_path) / (1024 * 1024)
            if size_mb <= max_size_mb:
                passed = True
                break
            logger.warning(
                f"GIF 转码后体积 {size_mb:.1f}MB > {max_size_mb}MB，"
                f"降档重试 (attempt={attempt})"
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"GIF 转码超时 (attempt={attempt})")
        except subprocess.CalledProcessError as e:
            logger.warning(
                f"GIF 转码子进程失败 (attempt={attempt}): {e.stderr.decode(errors='replace') if e.stderr else e}"
            )
        except Exception as e:
            logger.error(f"GIF 转码异常 (attempt={attempt}): {e}", exc_info=True)

        # 降档策略：先降 FPS，再降分辨率
        if attempt == 0:
            fps = max(5, fps - 3)
        elif attempt == 1:
            max_width = 240
            fps = _GIF_FPS

    if not passed:
        logger.warning("GIF 转码降档后仍不符合体积要求，将发送原始 mp4 作为视频")
    return passed


def _resolve_output_flag(
    metadata: Dict[str, Any],
    key: str,
    default: bool
) -> bool:
    value = metadata.get(key)
    if value is None:
        return bool(default)
    return bool(value)


def _append_media_skip_summary(text_parts: List[str], metadata: Dict[str, Any]) -> None:
    """将媒体跳过统计和逐项原因追加到文本节点。"""
    video_reasons = metadata.get('video_skip_reasons', []) or []
    image_reasons = metadata.get('image_skip_reasons', []) or []
    video_count = metadata.get('video_count', len(metadata.get('video_urls', [])))
    image_count = metadata.get('image_count', len(metadata.get('image_urls', [])))
    skipped_videos = [
        (idx + 1, reason)
        for idx, reason in enumerate(video_reasons)
        if reason
    ]
    skipped_images = [
        (idx + 1, reason)
        for idx, reason in enumerate(image_reasons)
        if reason
    ]
    if not skipped_videos and not skipped_images:
        return

    summary_parts = []
    if video_count:
        summary_parts.append(f"视频 {len(skipped_videos)}/{video_count}")
    if image_count:
        summary_parts.append(f"图片 {len(skipped_images)}/{image_count}")
    if summary_parts:
        text_parts.append(f"媒体跳过：{', '.join(summary_parts)}")

    for idx, reason in skipped_videos[:5]:
        text_parts.append(f"  视频[{idx}]：{reason}")
    for idx, reason in skipped_images[:5]:
        text_parts.append(f"  图片[{idx}]：{reason}")


def _mark_media_failure(
    metadata: Dict[str, Any],
    kind: str,
    index: int,
    reason: str
) -> None:
    """节点构建失败时回填跳过原因，供文本节点或调试使用。"""
    key = 'video_skip_reasons' if kind == 'video' else 'image_skip_reasons'
    modes_key = 'video_modes' if kind == 'video' else 'image_modes'
    count_key = 'failed_video_count' if kind == 'video' else 'failed_image_count'
    reasons = metadata.setdefault(key, [])
    while len(reasons) <= index:
        reasons.append(None)
    if not reasons[index]:
        reasons[index] = reason
    modes = metadata.setdefault(modes_key, [])
    while len(modes) <= index:
        modes.append('skip')
    modes[index] = 'skip'
    try:
        metadata[count_key] = int(metadata.get(count_key, 0) or 0) + 1
    except (TypeError, ValueError):
        metadata[count_key] = 1


def build_text_node(metadata: Dict[str, Any], max_video_size_mb: float = 0.0, enable_text_metadata: bool = True) -> Optional[Plain]:
    """构建文本节点

    Args:
        metadata: 元数据字典
        max_video_size_mb: 最大允许的视频大小(MB)，用于显示详细的错误信息
        enable_text_metadata: 是否包含视频图文文本信息的附加文本

    Returns:
        Plain文本节点，无内容时为None
    """
    if not enable_text_metadata:
        return None
        
    text_parts = []
    
    if metadata.get('title'):
        text_parts.append(f"标题：{metadata['title']}")
    if metadata.get('author'):
        text_parts.append(f"作者：{metadata['author']}")
    if metadata.get('desc'):
        text_parts.append(f"简介：{metadata['desc']}")
    if metadata.get('timestamp'):
        text_parts.append(f"发布时间：{metadata['timestamp']}")
    
    video_count = metadata.get('video_count', 0)
    if video_count > 0:
        actual_max_video_size_mb = metadata.get('max_video_size_mb')
        total_video_size_mb = metadata.get('total_video_size_mb', 0.0)
        
        if actual_max_video_size_mb is not None:
            if video_count == 1:
                text_parts.append(f"视频大小：{actual_max_video_size_mb:.1f} MB")
            else:
                text_parts.append(
                    f"视频大小：最大 {actual_max_video_size_mb:.1f} MB "
                    f"(共 {video_count} 个视频, 总计 {total_video_size_mb:.1f} MB)"
                )
    
    has_valid_media = metadata.get('has_valid_media')
    video_urls = metadata.get('video_urls', [])
    image_urls = metadata.get('image_urls', [])
    
    has_text_metadata = bool(
        metadata.get('title') or 
        metadata.get('author') or 
        metadata.get('desc') or 
        metadata.get('timestamp')
    )

    access_status = metadata.get("access_status")
    access_message = metadata.get("access_message")
    available_length_ms = metadata.get("available_length_ms")
    timelength_ms = metadata.get("timelength_ms")
    is_preview_only = metadata.get("is_preview_only")
    if access_status and access_status != "full" and access_message:
        text_parts.append(f"时长：{access_message}")
    elif is_preview_only and available_length_ms:
        try:
            available_seconds = max(0, int(available_length_ms) // 1000)
            full_seconds = (
                max(0, int(timelength_ms) // 1000)
                if timelength_ms is not None else
                None
            )
            available_min, available_sec = divmod(available_seconds, 60)
            if full_seconds is not None:
                full_min, full_sec = divmod(full_seconds, 60)
                text_parts.append(
                    f"时长：当前可解析 {available_min:02d}:{available_sec:02d} / "
                    f"全长 {full_min:02d}:{full_sec:02d}"
                )
            else:
                text_parts.append(
                    f"时长：当前可解析 {available_min:02d}:{available_sec:02d}"
                )
        except (TypeError, ValueError):
            pass

    hot_comments = metadata.get("hot_comments", [])
    if isinstance(hot_comments, list) and hot_comments:
        text_parts.append(f"热评（{len(hot_comments)}条）:")
        total = len(hot_comments)
        for idx, item in enumerate(hot_comments, start=1):
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "") or "").strip() or "未知用户"
            uid = str(item.get("uid", "") or "").strip()
            try:
                likes = int(item.get("likes", 0) or 0)
            except (TypeError, ValueError):
                likes = 0
            time_text = str(item.get("time", "") or "").strip() or "-"
            message = str(item.get("message", "") or "").strip() or "（无文本内容）"
            user_label = f"{username}(uid:{uid})" if uid else username
            text_parts.append(f"[{idx}] {user_label}")
            text_parts.append(f"点赞: {likes} | 时间: {time_text}")
            text_parts.append(message)
            if idx < total:
                text_parts.append("")
    
    if metadata.get('error'):
        text_parts.append(f"解析失败：{metadata['error']}")

    if has_valid_media is False and (video_urls or image_urls) and has_text_metadata and not metadata.get('exceeds_max_size'):
        if metadata.get('has_access_denied'):
            text_parts.append("解析失败：媒体访问被拒绝(403 Forbidden)")
        else:
            text_parts.append("解析失败：直链内未找到有效媒体")
    
    if metadata.get('exceeds_max_size'):
        actual_video_size = metadata.get('max_video_size_mb')
        if actual_video_size is not None:
            if max_video_size_mb > 0:
                text_parts.append(
                    f"解析失败：视频大小超过管理员设定的限制（{actual_video_size:.1f}MB > {max_video_size_mb:.1f}MB）"
                )
            else:
                text_parts.append(f"解析失败：视频大小超过限制（{actual_video_size:.1f}MB）")
    
    _append_media_skip_summary(text_parts, metadata)
    
    if metadata.get('url'):
        text_parts.append(f"原始链接：{metadata['url']}")
    
    if not text_parts:
        return None
    desc_text = "\n".join(text_parts)
    return Plain(desc_text)


def build_media_nodes(
    metadata: Dict[str, Any],
    use_local_files: bool = False,
    enable_rich_media: bool = True,
    gif_fps: int = _GIF_FPS,
    gif_max_size_mb: float = _GIF_MAX_SIZE_MB,
) -> List[Union[Image, Video]]:
    """构建媒体节点

    Args:
        metadata: 元数据字典
        use_local_files: 是否使用本地文件
        enable_rich_media: 是否构建富媒体节点
        gif_fps: GIF 转码帧率（来自 twitter_gif 配置）
        gif_max_size_mb: GIF 最大体积 (MB)（来自 twitter_gif 配置）

    Returns:
        媒体节点列表（Image或Video节点）
    """
    nodes = []
    url = metadata.get('url', '')

    if not enable_rich_media:
        logger.debug(f"富媒体输出已关闭，跳过媒体节点: {url}")
        return nodes
    
    if metadata.get('exceeds_max_size'):
        logger.debug(f"媒体超过大小限制，跳过节点构建: {url}")
        return nodes
    
    has_valid_media = metadata.get('has_valid_media')
    if has_valid_media is None:
        logger.warning(f"元数据中has_valid_media字段为None，视为False: {url}")
        has_valid_media = False
    
    if has_valid_media is False:
        logger.debug(f"媒体无效，跳过节点构建: {url}")
        return nodes
    
    video_urls = metadata.get('video_urls', [])
    image_urls = metadata.get('image_urls', [])
    file_paths = metadata.get('file_paths', [])
    video_modes = metadata.get('video_modes') or []
    image_modes = metadata.get('image_modes') or []
    use_fts = metadata.get('use_file_token_service', False)
    file_token_urls = metadata.get('file_token_urls', [])
    
    logger.debug(
        f"构建媒体节点: {url}, "
        f"视频: {len(video_urls)}, 图片: {len(image_urls)}, "
        f"文件路径: {len(file_paths)}, 使用本地文件: {use_local_files}, "
        f"文件Token服务: {use_fts}"
    )
    
    if not video_urls and not image_urls and not file_paths:
        logger.debug(f"无媒体内容，跳过节点构建: {url}")
        return nodes
    
    gif_video_indices = set(metadata.get('gif_video_indices') or [])
    file_idx = 0
    
    for idx, url_list in enumerate(video_urls):
        mode = video_modes[idx] if idx < len(video_modes) else (
            'local' if use_local_files else 'direct'
        )
        is_gif = idx in gif_video_indices
        media_label = 'gif' if is_gif else 'video'
        
        if mode == 'skip':
            file_idx += 1
            continue
        if not url_list or not isinstance(url_list, list):
            file_idx += 1
            continue
        
        media_url = url_list[0] if url_list else None
        if not media_url:
            file_idx += 1
            continue
        
        token_url = (
            file_token_urls[file_idx]
            if use_fts and file_idx < len(file_token_urls)
            else None
        )
        
        if is_gif:
            # GIF 视频：用 Image 组件发送，确保在 QQ 等平台显示为动图
            if token_url:
                try:
                    nodes.append(Image.fromURL(token_url))
                    file_idx += 1
                    continue
                except Exception as e:
                    logger.warning(f"使用Token URL构建GIF图片节点失败: {token_url}, 错误: {e}")
            
            if mode == 'local' and file_idx < len(file_paths) and file_paths[file_idx] and os.path.exists(file_paths[file_idx]):
                mp4_file = file_paths[file_idx]
                gif_file = os.path.splitext(mp4_file)[0] + '.gif'
                gif_ready = False
                if mp4_file.endswith('.mp4'):
                    if os.path.exists(gif_file):
                        gif_ready = True
                    else:
                        logger.info(f"开始将 GIF mp4 转码为 GIF 动图: {mp4_file}")
                        gif_ready = _convert_mp4_to_gif(
                            mp4_file, gif_file,
                            fps=gif_fps,
                            max_size_mb=gif_max_size_mb,
                        )
                if gif_ready:
                    try:
                        nodes.append(Image.fromFileSystem(gif_file))
                    except Exception as e:
                        logger.warning(f"构建GIF图片节点失败: {gif_file}, 错误: {e}")
                        _mark_media_failure(metadata, media_label, idx, f"构建本地GIF图片节点失败: {e}")
                else:
                    _mark_media_failure(metadata, media_label, idx, "本地GIF mp4转码失败，将发送原始视频")
                    # 降级为普通视频节点
                    try:
                        nodes.append(Video.fromFileSystem(mp4_file))
                    except Exception as e:
                        logger.warning(f"构建GIF降级视频节点失败: {mp4_file}, 错误: {e}")
            elif mode == 'local':
                _mark_media_failure(metadata, media_label, idx, "本地GIF图片文件不存在或不可访问")
            else:
                actual_media_url = strip_media_prefixes(media_url)
                try:
                    nodes.append(Image.fromURL(actual_media_url))
                except Exception as e:
                    logger.warning(f"构建GIF图片节点失败: {actual_media_url}, 错误: {e}")
                    _mark_media_failure(metadata, media_label, idx, f"构建GIF图片URL节点失败: {e}")
        else:
            # 普通视频
            if token_url:
                try:
                    nodes.append(Video.fromURL(token_url))
                    file_idx += 1
                    continue
                except Exception as e:
                    logger.warning(f"使用Token URL构建视频节点失败: {token_url}, 错误: {e}")
            
            if mode == 'local' and file_idx < len(file_paths) and file_paths[file_idx] and os.path.exists(file_paths[file_idx]):
                try:
                    nodes.append(Video.fromFileSystem(file_paths[file_idx]))
                except Exception as e:
                    logger.warning(f"构建视频节点失败: {file_paths[file_idx]}, 错误: {e}")
                    _mark_media_failure(metadata, media_label, idx, f"构建本地视频节点失败: {e}")
            elif mode == 'local':
                _mark_media_failure(metadata, media_label, idx, "本地视频文件不存在或不可访问")
            else:
                actual_media_url = strip_media_prefixes(media_url)
                try:
                    nodes.append(Video.fromURL(actual_media_url))
                except Exception as e:
                    logger.warning(f"构建视频节点失败: {actual_media_url}, 错误: {e}")
                    _mark_media_failure(metadata, media_label, idx, f"构建视频URL节点失败: {e}")
        
        file_idx += 1
    
    for image_idx, url_list in enumerate(image_urls):
        mode = image_modes[image_idx] if image_idx < len(image_modes) else (
            'local' if use_local_files else 'direct'
        )
        if mode == 'skip':
            file_idx += 1
            continue
        if not url_list or not isinstance(url_list, list):
            file_idx += 1
            continue
        
        image_url = url_list[0] if url_list else None
        if not image_url:
            file_idx += 1
            continue
        
        token_url = (
            file_token_urls[file_idx]
            if use_fts and file_idx < len(file_token_urls)
            else None
        )
        if token_url:
            try:
                nodes.append(Image.fromURL(token_url))
                file_idx += 1
                continue
            except Exception as e:
                logger.warning(f"使用Token URL构建图片节点失败: {token_url}, 错误: {e}")
        
        if mode == 'local' and file_idx < len(file_paths) and file_paths[file_idx]:
            try:
                nodes.append(Image.fromFileSystem(file_paths[file_idx]))
            except Exception as e:
                logger.warning(f"构建图片节点失败: {file_paths[file_idx]}, 错误: {e}")
                _mark_media_failure(metadata, 'image', image_idx, f"构建本地图片节点失败: {e}")
        elif mode == 'local':
            _mark_media_failure(metadata, 'image', image_idx, "本地图片文件不存在或不可访问")
        else:
            try:
                nodes.append(Image.fromURL(image_url))
            except Exception as e:
                logger.warning(f"构建图片节点失败: {image_url}, 错误: {e}")
                _mark_media_failure(metadata, 'image', image_idx, f"构建图片URL节点失败: {e}")
        
        file_idx += 1
    
    logger.debug(f"构建媒体节点完成: {url}, 共 {len(nodes)} 个节点")
    return nodes


def build_nodes_for_link(
    metadata: Dict[str, Any],
    use_local_files: bool = False,
    max_video_size_mb: float = 0.0,
    enable_text_metadata: bool = True,
    enable_rich_media: bool = True,
    gif_fps: int = _GIF_FPS,
    gif_max_size_mb: float = _GIF_MAX_SIZE_MB,
) -> List[Union[Plain, Image, Video]]:
    """构建单个链接的节点列表

    Args:
        metadata: 元数据字典
        use_local_files: 是否使用本地文件
        max_video_size_mb: 最大允许的视频大小(MB)，用于显示详细的错误信息
        enable_text_metadata: 是否发送图文文本消息
        enable_rich_media: 是否发送图片/视频
        gif_fps: GIF 转码帧率
        gif_max_size_mb: GIF 最大体积 (MB)

    Returns:
        节点列表（Plain、Image、Video对象）
    """
    nodes = []
    effective_text_metadata = _resolve_output_flag(
        metadata,
        "_enable_text_metadata",
        enable_text_metadata,
    )
    effective_rich_media = _resolve_output_flag(
        metadata,
        "_enable_rich_media",
        enable_rich_media,
    )

    media_nodes = build_media_nodes(
        metadata,
        use_local_files,
        effective_rich_media,
        gif_fps=gif_fps,
        gif_max_size_mb=gif_max_size_mb,
    )
    text_node = build_text_node(
        metadata,
        max_video_size_mb,
        effective_text_metadata,
    )
    if text_node:
        nodes.append(text_node)
    nodes.extend(media_nodes)
    
    return nodes


def is_pure_image_gallery(nodes: List[Union[Plain, Image, Video]]) -> bool:
    """判断节点列表是否是纯图片图集

    Args:
        nodes: 节点列表

    Returns:
        是否为纯图片图集
    """
    has_video = False
    has_image = False
    for node in nodes:
        if isinstance(node, Video):
            has_video = True
            break
        elif isinstance(node, Image):
            has_image = True
    return has_image and not has_video


def build_all_nodes(
    metadata_list: List[Dict[str, Any]],
    is_auto_pack: bool,
    large_video_threshold_mb: float = 0.0,
    max_video_size_mb: float = 0.0,
    enable_text_metadata: bool = True,
    enable_rich_media: bool = True,
    gif_fps: int = _GIF_FPS,
    gif_max_size_mb: float = _GIF_MAX_SIZE_MB,
) -> BuildAllNodesResult:
    """构建所有链接的节点，处理消息打包逻辑

    Args:
        metadata_list: 元数据列表
        is_auto_pack: 是否打包为Node
        large_video_threshold_mb: 大视频阈值(MB)
        max_video_size_mb: 最大允许的视频大小(MB)，用于显示错误信息
        enable_text_metadata: 是否发送图文文本消息
        enable_rich_media: 是否发送图片/视频
        gif_fps: GIF 转码帧率（来自 twitter_gif 配置）
        gif_max_size_mb: GIF 最大体积 (MB)（来自 twitter_gif 配置）

    Returns:
        BuildAllNodesResult 命名元组
    """
    all_link_nodes = []
    link_metadata = []
    temp_files = []
    video_files = []
    
    logger.debug(f"开始构建所有节点，元数据数量: {len(metadata_list)}, 打包模式: {is_auto_pack}")
    
    for idx, metadata in enumerate(metadata_list):
        url = metadata.get('url', '')
        max_video_size = metadata.get('max_video_size_mb')
        exceeds_max_size = metadata.get('exceeds_max_size', False)
        is_large_media = False
        if large_video_threshold_mb > 0 and max_video_size is not None and not exceeds_max_size:
            if max_video_size > large_video_threshold_mb:
                is_large_media = True
        
        use_local_files = metadata.get('use_local_files', False)
        
        logger.debug(
            f"构建节点[{idx}]: {url}, "
            f"大媒体: {is_large_media}, 使用本地文件: {use_local_files}"
        )
        
        link_nodes = build_nodes_for_link(
            metadata,
            use_local_files,
            gif_fps=gif_fps,
            gif_max_size_mb=gif_max_size_mb,
            max_video_size_mb=max_video_size_mb,
            enable_text_metadata=enable_text_metadata,
            enable_rich_media=enable_rich_media,
        )
        
        logger.debug(f"节点构建完成[{idx}]: {url}, 节点数量: {len(link_nodes)}")
        
        link_file_paths = metadata.get('file_paths', [])
        link_video_files = []
        link_temp_files = []
        
        video_urls = metadata.get('video_urls', [])
        video_count = len(video_urls)
        video_modes = metadata.get('video_modes') or []
        image_modes = metadata.get('image_modes') or []

        for fp_idx, file_path in enumerate(link_file_paths):
            if not file_path:
                continue
            if fp_idx < video_count:
                mode = video_modes[fp_idx] if fp_idx < len(video_modes) else ''
                if mode == 'local':
                    link_video_files.append(file_path)
                    video_files.append(file_path)
            else:
                img_idx = fp_idx - video_count
                mode = image_modes[img_idx] if img_idx < len(image_modes) else ''
                if mode == 'local':
                    link_temp_files.append(file_path)
                    temp_files.append(file_path)
        
        if link_nodes:
            all_link_nodes.append(link_nodes)
            link_metadata.append(LinkBuildMeta(
                link_nodes=link_nodes,
                is_large_media=is_large_media,
                is_normal=not is_large_media,
                video_files=link_video_files,
                temp_files=link_temp_files,
            ))
        else:
            logger.debug(f"节点为空，跳过发送队列: {url}")
    
    logger.debug(
        f"所有节点构建完成: "
        f"链接节点: {len(all_link_nodes)}, "
        f"临时文件: {len(temp_files)}, "
        f"视频文件: {len(video_files)}"
    )
    
    return BuildAllNodesResult(all_link_nodes, link_metadata, temp_files, video_files)

