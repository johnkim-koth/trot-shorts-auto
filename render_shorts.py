import os
import re
import json
import base64
import asyncio
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
import yt_dlp
from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    from moviepy import ImageClip, AudioFileClip, CompositeAudioClip, concatenate_audioclips
    from moviepy.audio.fx import AudioFadeOut, MultiplyVolume
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, CompositeAudioClip, concatenate_audioclips
    from moviepy.audio.fx.all import audio_fadeout, volumex
    MOVIEPY_V2 = False

def safe_duration(clip, dur):
    dur = float(dur)
    if hasattr(clip, "with_duration"):
        clip = clip.with_duration(dur)
    elif hasattr(clip, "set_duration"):
        clip = clip.set_duration(dur)
    clip.duration = dur
    return clip

def safe_audio(clip, audio_clip):
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio_clip)
    return clip.set_audio(audio_clip)

def safe_start(clip, start_t):
    if hasattr(clip, "with_start"):
        return clip.with_start(float(start_t))
    return clip.set_start(float(start_t))

def safe_subclip(clip, start_t, end_t):
    if hasattr(clip, "subclipped"):
        return clip.subclipped(float(start_t), float(end_t))
    return clip.subclip(float(start_t), float(end_t))

def safe_volumex(clip, factor):
    if MOVIEPY_V2:
        return clip.with_effects([MultiplyVolume(factor)])
    return clip.fx(volumex, factor)

def safe_fadeout(clip, fade_dur):
    if MOVIEPY_V2:
        return clip.with_effects([AudioFadeOut(fade_dur)])
    return clip.fx(audio_fadeout, fade_dur)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly"
]
sa_info = json.loads(os.environ["GCP_SA_KEY"])
creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
gc = gspread.authorize(creds)

WEBHOOK_URL = os.environ.get("APPS_SCRIPT_WEBHOOK", "").strip()
if not WEBHOOK_URL:
    raise RuntimeError("APPS_SCRIPT_WEBHOOK 시크릿이 설정되지 않았습니다.")

SPREADSHEET_NAME = "Copy of 블로그 자동화 관리 시트"
sheet = gc.open(SPREADSHEET_NAME).worksheet("최신곡")

def get_melon_album_art(melon_song_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        if not melon_song_url or not melon_song_url.startswith("http"):
            return None
        res = requests.get(melon_song_url, headers=headers, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            meta_img = soup.find("meta", property="og:image")
            if meta_img and meta_img.get("content"):
                img_url = meta_img["content"].split("?")[0]
                img_url = re.sub(r"resize/\d+", "resize/500", img_url)
                return img_url
    except Exception as e:
        print(f"      [안내] 멜론 크롤링 예외: {e}")
    return None

async def _async_generate_voice(text, filename="voice.mp3"):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural", rate="+5%")
    await communicate.save(filename)

def generate_voice_sync(text, filename="voice.mp3"):
    asyncio.run(_async_generate_voice(text, filename))

def fetch_song_highlight_audio(singer, song, out_filename="bgm_raw.m4a"):
    if os.path.exists(out_filename):
        os.remove(out_filename)

    query = f"ytsearch1:{singer} {song} 공식 음원 audio"
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out_filename,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 10,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(query, download=True)
            if os.path.exists(out_filename) and os.path.getsize(out_filename) > 10000:
                return out_filename
    except Exception as e:
        print(f"      [안내] 신곡 배경음 추출 건너뜀 (나레이션 단독 모드 전환): {e}")
    return None

def get_font_safe(size):
    candidates = [
        "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

def create_optimized_shorts(singer, song, album_url, output_path="shorts.mp4"):
    print("   [1/4] Edge-TTS AI 아나운서 음성 생성 중...")
    script_text = f"오늘 전해드릴 명품 트로트 신곡은 가수 {singer}의 {song}입니다. 전곡 감상과 가사는 왼쪽 아래 동그란 사진을 누르고 맨 위 링크를 확인하세요."
    audio_path = "voice.mp3"
    generate_voice_sync(script_text, audio_path)

    raw_voice_clip = AudioFileClip(audio_path)
    voice_dur = round(max(float(raw_voice_clip.duration) - 0.08, 3.0), 2)
    voice_clip = safe_subclip(raw_voice_clip, 0, voice_dur)

    print("   [2/4] 신곡 하이라이트 음원 추출 및 오디오 믹싱 중...")
    bgm_raw_path = fetch_song_highlight_audio(singer, song, "bgm_raw.m4a")
    bgm_clip_full = None

    if bgm_raw_path:
        try:
            bgm_clip_full = AudioFileClip(bgm_raw_path)
            raw_bgm_dur = float(bgm_clip_full.duration)
            target_total = max(voice_dur + 8.5, 15.5)

            if raw_bgm_dur > target_total + 45:
                start_sec = 45.0
            elif raw_bgm_dur > target_total + 15:
                start_sec = 15.0
            else:
                start_sec = 0.0

            end_sec = round(min(start_sec + target_total, raw_bgm_dur - 0.1), 2)
            bgm_sub = safe_subclip(bgm_clip_full, start_sec, end_sec)
            duration = round(end_sec - start_sec, 2)

            if duration > voice_dur + 1.0:
                bgm_part1 = safe_volumex(safe_subclip(bgm_sub, 0, voice_dur), 0.20)
                bgm_part2 = safe_volumex(safe_subclip(bgm_sub, voice_dur, duration), 1.00)
                bgm_ducked = concatenate_audioclips([bgm_part1, bgm_part2])
            else:
                bgm_ducked = safe_volumex(bgm_sub, 0.35)

            bgm_ducked = safe_fadeout(bgm_ducked, 1.5)
            final_audio = CompositeAudioClip([bgm_ducked, safe_start(voice_clip, 0)])
            final_audio = safe_duration(final_audio, duration)
            print(f"      └ 오디오 믹싱 성공! (총 {duration:.1f}초: 나레이션 + 하이라이트 결합)")
        except Exception as mix_err:
            print(f"      [안내] 오디오 믹싱 예외 발생, 기본 음성으로 안전 전환: {mix_err}")
            duration = voice_dur
            final_audio = CompositeAudioClip([safe_start(voice_clip, 0)])
            final_audio = safe_duration(final_audio, duration)
    else:
        duration = voice_dur
        final_audio = CompositeAudioClip([safe_start(voice_clip, 0)])
        final_audio = safe_duration(final_audio, duration)
        print(f"      └ 기본 나레이션 단독 모드 적용 ({duration:.1f}초)")

    print("   [3/4] 공식 앨범 자켓 및 5060 맞춤 화면 구성 중...")
    jacket_img = None
    if album_url and album_url.startswith("http"):
        try:
            res = requests.get(album_url, timeout=8)
            jacket_img = Image.open(BytesIO(res.content)).convert("RGB")
        except Exception:
            jacket_img = None

    if jacket_img is None:
        jacket_img = Image.new("RGB", (720, 720), color=(45, 35, 60))
        d_temp = ImageDraw.Draw(jacket_img)
        f_temp = get_font_safe(55)
        d_temp.text((360, 320), singer, font=f_temp, fill=(255, 255, 255), anchor="mm")
        d_temp.text((360, 400), song, font=f_temp, fill=(255, 215, 0), anchor="mm")

    jacket_img = jacket_img.resize((720, 720), Image.Resampling.LANCZOS)

    bg_img = jacket_img.resize((1080, 1920), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(radius=45))
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 135))
    bg_img = Image.alpha_composite(bg_img.convert("RGBA"), overlay).convert("RGB")

    border = Image.new("RGB", (728, 728), (255, 255, 255))
    border.paste(jacket_img, (4, 4))
    bg_img.paste(border, (176, 460))

    draw = ImageDraw.Draw(bg_img)
    f_bold = get_font_safe(60)
    f_sub = get_font_safe(44)
    f_guide = get_font_safe(36)

    draw.text((540, 240), "[ 신곡 하이라이트 감상 ]", font=f_sub, fill=(255, 215, 0), anchor="mm")
    draw.text((540, 340), f"{song}", font=f_bold, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 1240), f"가수: {singer}", font=f_bold, fill=(255, 255, 255), anchor="mm")

    draw.rectangle([(80, 1560), (1000, 1710)], fill=(215, 38, 56))
    draw.text((540, 1610), "[전곡 듣기 & 가사 전문 감상실]", font=f_guide, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 1665), "◀◀ 왼쪽 아래 [채널 사진] 누르고 상단 링크 클릭!", font=f_guide, fill=(255, 235, 59), anchor="mm")

    frame_path = "frame.png"
    bg_img.save(frame_path)

    print(f"   [4/4] MP4 비디오 렌더링 인코딩 중 ({duration}초, yuv420p 표준 호환)...")
    clip = safe_duration(ImageClip(frame_path), duration)
    clip = safe_audio(clip, final_audio)
    clip.fps = 24
    clip.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger=None
    )

    try:
        raw_voice_clip.close()
        if bgm_clip_full:
            bgm_clip_full.close()
        clip.close()
    except Exception:
        pass

    for temp_f in [audio_path, frame_path, "bgm_raw.m4a"]:
        if os.path.exists(temp_f):
            os.remove(temp_f)

    return output_path

def upload_via_apps_script(file_path, file_name, row_num, new_id):
    with open(file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "action": "upload_shorts",
        "fileName": file_name,
        "fileBase64": b64_data,
        "rowNum": row_num,
        "newId": new_id
    }
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=120, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"Apps Script HTTP 오류: {resp.status_code} / {resp.text[:200]}")

    result = resp.json()
    if result.get("status") != "success":
        raise RuntimeError(f"Apps Script 내부 처리 오류: {result.get('message') or result.get('error')}")
    return result.get("fileId")

def run_pipeline():
    rows = sheet.get_all_values()
    print("[시작] '최신곡' 시트에서 작업 대상을 탐색합니다...")

    kst = timezone(timedelta(hours=9))
    prefix = datetime.now(kst).strftime("%m%d")

    current_numbers = []
    for r in rows:
        val = str(r[1]).strip() if len(r) > 1 else ""
        if val.startswith(prefix + "-"):
            try:
                num = int(val.split("-")[1])
                current_numbers.append(num)
            except Exception:
                pass
    next_seq = (max(current_numbers) + 1) if current_numbers else 1
    new_id = f"{prefix}-{next_seq}"

    processed = False
    for idx in range(3, len(rows)):
        row_num = idx + 1
        singer = rows[idx][2].strip() if len(rows[idx]) > 2 else ""
        song = rows[idx][3].strip() if len(rows[idx]) > 3 else ""
        melon_url = rows[idx][4].strip() if len(rows[idx]) > 4 else ""
        status = rows[idx][7].strip() if len(rows[idx]) > 7 else ""
        shorts_id = rows[idx][15].strip() if len(rows[idx]) > 15 else ""
        content_html = rows[idx][6] if len(rows[idx]) > 6 else ""

        if status == "Published" and not shorts_id:
            processed = True
            print(f"\n[대상 포착] {row_num}행: {singer} - {song} (발행 ID: {new_id})")

            jacket_url = get_melon_album_art(melon_url)
            if not jacket_url:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
                jacket_url = m.group(1) if m else None

            clean_singer = "".join(c for c in singer if c.isalnum() or c in (" ", "_")).strip()
            clean_song = "".join(c for c in song if c.isalnum() or c in (" ", "_")).strip()
            video_filename = f"Shorts_{new_id}_{clean_singer}_{clean_song}.mp4"

            try:
                rendered = create_optimized_shorts(singer, song, jacket_url, video_filename)
                print("   [전송] 대표님 Google Drive(Shorts_Auto) 및 시트 동기화 요청 중...")
                drive_id = upload_via_apps_script(rendered, video_filename, row_num, new_id)

                print(f"\n[성공] {row_num}행: ID [{new_id}] / Drive ID [{drive_id}] / 'Shorts_Ready' 확정 완료!")
                if os.path.exists(rendered):
                    os.remove(rendered)
                break

            except Exception as e:
                print(f"[전송/렌더링 실패] {str(e)}")
                break

    if not processed:
        print("[안내] 현재 처리 대기 중인 대상(Published & File ID 공란)이 없습니다.")

if __name__ == "__main__":
    run_pipeline()
