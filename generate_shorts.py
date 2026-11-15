# -*- coding: utf-8 -*-
"""
=============================================================================
[유튜브 쇼츠 자동 생성 & 업로드 무결 마스터 코드 - generate_shorts.py]
1. 시트 격리 락: 티스토리 발행 완료('Published') & 쇼츠 미발행(Q열 공백) 행만 포착
2. Duration 락: moviepy v1/v2 전 클립(배경/자켓/자막/오디오) duration 강제 고정
3. TTS & BGM 락: gTTS 감성 후킹 음성 + 멜론 500px 고화질 자켓 모션 렌더링
4. 업로드 & 웹훅 락: YouTube Data API v3 쇼츠 자동 업로드 후 시트 P/Q열 자동 기록
=============================================================================
"""

import os
import re
import csv
import io
import time
import json
import urllib.parse
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from gtts import gTTS

# moviepy v1 / v2 완벽 호환 임포트
try:
    from moviepy import ImageClip, AudioFileClip, CompositeVideoClip
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
    MOVIEPY_V2 = False

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 1. 기본 환경 설정 (블로그 파이프라인과 완전 격리)
CONFIG = {
    "SHEET_CSV_URL": "https://docs.google.com/spreadsheets/d/1vF2VhijXr8Yu50E104DOi8GhhmKEmKefkeV7_uHixwk/export?format=csv&gid=0",
    "WEBHOOK_URL": os.environ.get("GOOGLE_WEBHOOK_URL", "https://script.google.com/macros/s/AKfycbwE_RYrGAlULk2rfju5aSbdBcotjzBV-Kgr4WWVuGL5TWLVCkccX6Caz7Txu1QjSvDGpQ/exec"),
    "YT_CLIENT_ID": os.environ.get("YT_CLIENT_ID", "").strip(),
    "YT_CLIENT_SECRET": os.environ.get("YT_CLIENT_SECRET", "").strip(),
    "YT_REFRESH_TOKEN": os.environ.get("YT_REFRESH_TOKEN", "").strip(),
    "VIDEO_W": 1080,
    "VIDEO_H": 1920,
    "FPS": 24
}

def set_clip_duration(clip, dur):
    """moviepy v1(.set_duration) 및 v2(.with_duration) 모두 100% duration 속성 보장"""
    dur = float(dur)
    if hasattr(clip, "with_duration"):
        clip = clip.with_duration(dur)
    elif hasattr(clip, "set_duration"):
        clip = clip.set_duration(dur)
    clip.duration = dur
    return clip

def set_clip_audio(clip, audio_clip):
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio_clip)
    return clip.set_audio(audio_clip)

def set_clip_position(clip, pos):
    if hasattr(clip, "with_position"):
        return clip.with_position(pos)
    return clip.set_position(pos)

def set_clip_start(clip, start_t):
    if hasattr(clip, "with_start"):
        return clip.with_start(float(start_t))
    return clip.set_start(float(start_t))

# 2. 시트에서 쇼츠 제작 대상 탐색 (블로그 'Published' 완료 + 쇼츠 Q열 미등록 행만 포착)
def fetch_shorts_target_from_sheet():
    print("[시작] '최신곡' 시트에서 쇼츠 제작 대상을 탐색합니다...")
    res = requests.get(f"{CONFIG['SHEET_CSV_URL']}&_t={int(time.time())}", timeout=15)
    res.encoding = 'utf-8'
    rows = list(csv.reader(io.StringIO(res.text)))

    # 열 구조: A(0)=ID, C(2)=가수, D(3)=제목, E(4)=Melon Link, G(6)=Draft/Content, H(7)=Status, P(15)=Shorts File ID, Q(16)=Shorts Link
    for idx in range(4, len(rows)):
        row = rows[idx]
        if len(row) <= 7:
            continue

        row_id = row[0].strip() if len(row) > 0 else f"ROW-{idx+1}"
        artist = row[2].strip() if len(row) > 2 else ""
        title = row[3].strip() if len(row) > 3 else ""
        melon_url = row[4].strip() if len(row) > 4 else ""
        raw_content = row[6].strip() if len(row) > 6 else ""
        status = row[7].strip() if len(row) > 7 else ""
        shorts_link = row[16].strip() if len(row) > 16 else ""

        if not artist or not title or artist.startswith("http"):
            continue

        # 티스토리 발행 완료('Published') 상태이면서 쇼츠 링크가 아직 없는 곡만 선택 (블로그 작업과 충돌 제로)
        if status.lower() == "published" and not shorts_link.startswith("http"):
            return {
                "row_idx": idx + 1,
                "id": row_id,
                "artist": artist,
                "title": title,
                "melon_url": melon_url,
                "raw_content": raw_content
            }
    return None

# 3. 멜론 500px 고화질 자켓 이미지 확보
def download_melon_jacket(artist, title, melon_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    album_id = ""
    if melon_url:
        m = re.search(r"albumId=(\d+)", melon_url)
        if m:
            album_id = m.group(1)

    if not album_id:
        try:
            q = urllib.parse.quote(f"{artist} {title}")
            res = requests.get(f"https://www.melon.com/search/total/index.htm?q={q}", headers=headers, timeout=8)
            links = re.findall(r"goAlbumDetail\('(\d+)'\)", res.text) or re.findall(r"albumId=(\d+)", res.text)
            if links:
                album_id = links[0]
        except Exception:
            pass

    jacket_path = "/tmp/shorts_jacket.jpg"
    if album_id:
        try:
            res = requests.get(f"https://www.melon.com/album/detail.htm?albumId={album_id}", headers=headers, timeout=8)
            og = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', res.text)
            if og:
                img_url = og.group(1).split("?")[0]
                img_res = requests.get(img_url, headers=headers, timeout=10)
                if img_res.status_code == 200 and len(img_res.content) > 2000:
                    with open(jacket_path, "wb") as f:
                        f.write(img_res.content)
                    return jacket_path
        except Exception:
            pass

    # 폴백 기본 캔버스 생성
    img = Image.new("RGB", (600, 600), (30, 41, 59))
    img.save(jacket_path, "JPEG")
    return jacket_path

def load_korean_font(size):
    font_candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

# 4. MP4 모션 비디오 렌더링 (모든 레이어 duration 명시 고정)
def render_shorts_video(target):
    artist = target["artist"]
    title = target["title"]

    print("    [1/3] 감성 후킹 AI 음성 생성 중...")
    narration = (
        f"가슴을 울리는 트로트 명곡! {artist}의 {title}입니다. "
        f"깊은 감성과 호소력 짙은 보이스가 돋보이는 이 곡의 전체 가사와 노래 듣기, "
        f"그리고 {artist}의 상세 프로필은 아래 설명란 링크에서 바로 확인하실 수 있습니다."
    )
    tts_path = "/tmp/shorts_tts.mp3"
    tts = gTTS(text=narration, lang="ko")
    tts.save(tts_path)

    audio_clip = AudioFileClip(tts_path)
    total_duration = round(max(float(audio_clip.duration) + 0.8, 12.0), 2)
    audio_clip = set_clip_duration(audio_clip, total_duration)

    print("    [2/3] 공식 앨범 자켓 및 세로형(1080x1920) 프레임 구성 중...")
    jacket_path = download_melon_jacket(artist, title, target.get("melon_url", ""))
    raw_jacket = Image.open(jacket_path).convert("RGB")

    # 1) 배경 블러 이미지 생성
    bg_img = ImageOps.fit(raw_jacket, (CONFIG["VIDEO_W"], CONFIG["VIDEO_H"]), centering=(0.5, 0.5))
    bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=28))
    dark_overlay = Image.new("RGBA", (CONFIG["VIDEO_W"], CONFIG["VIDEO_H"]), (10, 15, 28, 175))
    bg_rgba = Image.alpha_composite(bg_img.convert("RGBA"), dark_overlay).convert("RGB")

    # 2) 상단 타이틀 배지 + 중앙 앨범 자켓 카드 + 하단 안내 자막 합성
    draw = ImageDraw.Draw(bg_rgba)
    f_badge = load_korean_font(40)
    f_title = load_korean_font(64)
    f_artist = load_korean_font(52)
    f_sub = load_korean_font(38)

    # 상단 배지
    draw.rounded_rectangle([240, 160, 840, 240], radius=40, fill=(217, 119, 6))
    draw.text((540, 200), "오늘의 추천 트로트 명곡", font=f_badge, fill=(255, 255, 255), anchor="mm")

    # 곡명 & 가수명
    display_title = title if len(title) <= 16 else title[:15] + "…"
    draw.text((540, 330), display_title, font=f_title, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 420), f"🎤 {artist}", font=f_artist, fill=(253, 224, 71), anchor="mm")

    # 중앙 앨범 자켓 (680x680)
    card_size = 680
    jacket_card = ImageOps.fit(raw_jacket, (card_size, card_size), centering=(0.5, 0.5))
    card_x = (CONFIG["VIDEO_W"] - card_size) // 2
    card_y = 520
    draw.rounded_rectangle([card_x - 10, card_y - 10, card_x + card_size + 10, card_y + card_size + 10], radius=28, fill=(255, 255, 255))
    bg_rgba.paste(jacket_card, (card_x, card_y))

    # 하단 가사/풀버전 유도 배너
    draw.rounded_rectangle([120, 1300, 960, 1480], radius=24, fill=(30, 41, 59))
    draw.text((540, 1360), "전체 가사 · 작사/작곡 크레딧 · 풀영상", font=f_sub, fill=(147, 197, 253), anchor="mm")
    draw.text((540, 1425), "▼ 설명란 & 댓글 링크에서 바로 듣기 ▼", font=f_sub, fill=(255, 255, 255), anchor="mm")

    frame_path = "/tmp/shorts_frame.png"
    bg_rgba.save(frame_path, "PNG")

    print(f"    [3/3] MP4 모션 비디오 렌더링 인코딩 중 ({total_duration}초)...")
    output_mp4 = "/tmp/output_shorts.mp4"

    # 모든 클립에 duration을 명시적으로 설정하여 'Attribute duration not set' 원천 차단
    base_clip = ImageClip(frame_path)
    base_clip = set_clip_duration(base_clip, total_duration)

    final_video = CompositeVideoClip([base_clip], size=(CONFIG["VIDEO_W"], CONFIG["VIDEO_H"]))
    final_video = set_clip_duration(final_video, total_duration)
    final_video = set_clip_audio(final_video, audio_clip)

    final_video.write_videofile(
        output_mp4,
        fps=CONFIG["FPS"],
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        threads=2,
        logger=None
    )

    try:
        audio_clip.close()
        final_video.close()
    except Exception:
        pass

    print(f"-> [렌더링 성공] {output_mp4} 생성 완료 ({total_duration}초)")
    return output_mp4

# 5. YouTube Data API v3 쇼츠 자동 업로드
def upload_to_youtube_shorts(mp4_path, target):
    if not all([CONFIG["YT_CLIENT_ID"], CONFIG["YT_CLIENT_SECRET"], CONFIG["YT_REFRESH_TOKEN"]]):
        print("-> [경고] YouTube API 인증 정보(YT_CLIENT_ID/SECRET/REFRESH_TOKEN)가 비어 있습니다.")
        return "", ""

    creds = Credentials(
        token=None,
        refresh_token=CONFIG["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CONFIG["YT_CLIENT_ID"],
        client_secret=CONFIG["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    artist = target["artist"]
    title = target["title"]
    tag_url = f"https://trotmusic.tistory.com/tag/{urllib.parse.quote(artist)}"

    yt_title = f"{artist} - {title} 가사 노래듣기 #트로트 #Shorts"[:98]
    yt_desc = (
        f"🎤 가수 : {artist}\n"
        f"🎵 곡명 : {title}\n\n"
        f"▶ {artist} - {title} 전체 가사, 작사/작곡 크레딧 및 전곡 모음 듣기:\n"
        f"{tag_url}\n\n"
        f"#{artist} #{title} #트로트 #트로트신곡 #성인가요 #Shorts"
    )

    body = {
        "snippet": {
            "title": yt_title,
            "description": yt_desc,
            "tags": [artist, title, f"{artist} 노래", "트로트", "트로트신곡", "Shorts"],
            "categoryId": "10"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(mp4_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = req.execute()
    video_id = response.get("id", "")
    shorts_url = f"https://www.youtube.com/shorts/{video_id}" if video_id else ""
    print(f"-> [유튜브 쇼츠 업로드 성공] {shorts_url}")
    return video_id, shorts_url

# 6. 구글 시트 웹훅 연동 (쇼츠 완료 상태 업데이트)
def update_shorts_status_via_webhook(target, video_id, shorts_url):
    webhook_url = CONFIG["WEBHOOK_URL"].strip()
    if not webhook_url:
        return
    payload = {
        "action": "update_shorts",
        "singer": target["artist"],
        "title": target["title"],
        "row_idx": target["row_idx"],
        "shorts_file_id": video_id,
        "shorts_link": shorts_url
    }
    try:
        res = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        print(f"-> [시트 갱신 완료] {res.text}")
    except Exception as e:
        print(f"-> 시트 웹훅 전송 참고: {e}")

if __name__ == "__main__":
    print("=" * 60)
    print("★ [유튜브 쇼츠 자동 발행 시스템] 무결 마스터 가동")
    print("=" * 60)

    target = fetch_shorts_target_from_sheet()
    if not target:
        print("쇼츠 제작 대기(블로그 Published 완료 & 쇼츠 미발행) 곡이 없습니다. 종료합니다.")
        exit(0)

    print(f"🎯 [대상 포착] {target['row_idx']}행: {target['artist']} - {target['title']} (ID: {target['id']})")
    try:
        mp4_file = render_shorts_video(target)
        vid, s_url = upload_to_youtube_shorts(mp4_file, target)
        if vid:
            update_shorts_status_via_webhook(target, vid, s_url)
    except Exception as e:
        print(f"❌ 렌더링/업로드 실패: {e}")
        exit(1)
