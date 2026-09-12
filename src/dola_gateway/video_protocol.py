"""Active Dola video polling and download helpers."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import aiohttp

from . import config


POLL_JS = r"""
async ({conversationId, msToken, fp}) => {
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.61", doubao_pc_version: "3.32.61",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });

  const resp = await fetch("/im/chain/single?" + params.toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json; encoding=utf-8",
      "agw-js-conv": "str",
      "Accept": "*/*",
    },
    body: JSON.stringify({
      cmd: 3100,
      uplink_body: {
        pull_singe_chain_uplink_body: {
          conversation_id: conversationId,
          anchor_index: Number.MAX_SAFE_INTEGER,
          conversation_type: 3,
          direction: 1,
          limit: 20,
          ext: {},
          filter: {index_list: []},
          evaluate_ab_params: "",
          evaluate_common_params: "",
        },
      },
      sequence_id: crypto.randomUUID(),
      channel: 2,
      version: "1",
    }),
    credentials: "include",
  });
  if (!resp.ok) return {ok: false, status: resp.status, texts: [], videos: []};

  const data = await resp.json();
  const messages =
    (((data.downlink_body || {}).pull_singe_chain_downlink_body) || {}).messages || [];
  const texts = [];
  const videos = [];
  const videoModels = [];
  for (const msg of messages) {
    let content = msg.content;
    if (typeof content === "string") {
      try { content = JSON.parse(content); } catch (e) { continue; }
    }
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      const text = (((block.content || {}).text_block) || {}).text || "";
      if (text) texts.push(text.slice(0, 120));
      if (block.block_type !== 2074) continue;
      const creations = (((block.content || {}).creation_block) || {}).creations || [];
      for (const creation of creations) {
        if (creation.type !== 2) continue;
        const url = ((creation.video || {}).download_url) || "";
        if (url.startsWith("http")) {
          videos.push(url);
          videoModels.push((creation.video || {}).video_model || "");
        }
      }
    }
  }
  return {ok: true, status: resp.status, texts, videos, videoModels};
}
"""


def extract_unwatermarked_url(video_model_str: str, fallback_url: str) -> str:
    """Return the highest-bitrate embedded source URL when available."""
    try:
        model = json.loads(video_model_str or "{}")
        candidates = []
        for item in (model.get("video_list") or {}).values():
            if not isinstance(item, dict) or not item.get("main_url"):
                continue
            try:
                decoded = base64.b64decode(item["main_url"]).decode("utf-8", "ignore")
            except Exception:
                continue
            if decoded.startswith("http"):
                bitrate = int(item.get("bitrate") or item.get("real_bitrate") or 0)
                candidates.append((bitrate, decoded))
        if candidates:
            return max(candidates)[1]
    except Exception:
        pass
    return fallback_url


async def download_video(url: str, account: str) -> Path:
    """Download a completed video into the configured state directory."""
    download_dir = Path(config.DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    destination = download_dir / f"{account}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, proxy=config.PROXY or None) as response:
            response.raise_for_status()
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(1 << 16):
                    stream.write(chunk)
    return destination
