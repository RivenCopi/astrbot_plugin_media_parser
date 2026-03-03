from typing import TypedDict, List, Dict, Optional, Any

class MediaMetadata(TypedDict, total=False):
    """提取的媒体元数据信息结构"""
    url: str
    title: Optional[str]
    author: Optional[str]
    desc: Optional[str]
    timestamp: Optional[str]
    video_urls: List[List[str]]
    image_urls: List[List[str]]
    image_headers: Dict[str, str]
    video_headers: Dict[str, str]
    video_force_download: Optional[bool]
    error: Optional[str]
    is_normal: Optional[bool]
    is_large_media: Optional[bool]
    link_nodes: Optional[List[Any]]
    video_files: Optional[List[str]]
