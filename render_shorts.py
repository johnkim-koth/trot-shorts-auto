import os
import re
import json
import base64
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
import yt_dlp
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# ================= [LOCKED: moviepy v1/v2 호환 래퍼] =================
try:
    from moviepy import ImageClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips
    from moviepy.audio.fx import AudioFadeOut, MultiplyVolume
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips
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

def safe_position(clip, pos):
    if hasattr(clip, "with_position"):
        return clip.with_position(pos)
    return clip.set_position(pos)

def safe_resize(clip, scale_func):
    if hasattr(clip, "resized"):
        return clip.resized(scale_func)
    return clip.resize(scale_func)

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

# ================= [LOCKED: 시트/웹훅 인증 및 설정] =================
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
spreadsheet = gc.open(SPREADSHEET_NAME)

# ================= [VERIFIED: 3중 백업 고화질 앨범 자켓 확보 엔진] =================
def get_best_album_art(singer, song, melon_song_url, content_html):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    def _download_valid_image(url):
        if not url or not isinstance(url, str) or not url.startswith("http"):
            return None
        try:
            clean_url = url.split("?")[0]
            clean_url = re.sub(r"resize/\d+", "resize/500", clean_url)
            res = requests.get(clean_url, headers=headers, timeout=8)
            if res.status_code == 200 and len(res.content) > 3000:
                img = Image.open(BytesIO(res.content)).convert("RGB")
                if img.width >= 150 and img.height >= 150:
                    return img
        except Exception:
            pass
        return None

    # 1순위: 시트 E열(Melon Album Link) 직접 크롤링
    if melon_song_url and isinstance(melon_song_url, str) and melon_song_url.startswith("http"):
        try:
            res = requests.get(melon_song_url, headers=headers, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                meta_img = soup.find("meta", property="og:image")
                if meta_img and meta_img.get("content"):
                    img = _download_valid_image(meta_img["content"])
                    if img:
                        return img
        except Exception:
            pass

    # 2순위: 멜론 검색으로 앨범 상세페이지 역추적
    try:
        q = urllib.parse.quote(f"{singer} {song}")
        res = requests.get(f"https://www.melon.com/search/total/index.htm?q={q}", headers=headers, timeout=8)
        album_ids = re.findall(r"goAlbumDetail\('(\d+)'\)", res.text) or re.findall(r"albumId=(\d+)", res.text)
        if album_ids:
            detail_res = requests.get(f"https://www.melon.com/album/detail.htm?albumId={album_ids[0]}", headers=headers, timeout=8)
            soup = BeautifulSoup(detail_res.text, "html.parser")
            meta_img = soup.find("meta", property="og:image")
            if meta_img and meta_img.get("content"):
                img = _download_valid_image(meta_img["content"])
                if img:
                    return img
    except Exception:
        pass

    # 3순위: 시트 G열(Draft/Content) 본문 HTML 내 삽입된 이미지 백업 추출
    if content_html and isinstance(content_html, str):
        img_urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
        for u in img_urls:
            if "unsplash.com" in u:
                continue
            img = _download_valid_image(u)
            if img:
                return img

    return None

async def _async_generate_voice(text, filename="voice.mp3"):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural", rate="+8%")
    await communicate.save(filename)

def generate_voice_sync(text, filename="voice.mp3"):
    asyncio.run(_async_generate_voice(text, filename))

# ================= [VERIFIED: 보조 계정 YT_COOKIES 연동 원곡 하이라이트 음원 추출기] =================
def fetch_song_highlight_audio(singer, song, out_filename="bgm_raw.m4a"):
    if os.path.exists(out_filename):
        os.remove(out_filename)

    cookie_path = "/tmp/yt_cookies.txt"
    yt_cookies_env = os.environ.get("YT_COOKIES", "").strip()
    has_cookie = False

    if yt_cookies_env:
        try:
            with open(cookie_path, "w", encoding="utf-8") as cf:
                cf.write(yt_cookies_env + "\n")
            if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 50:
                has_cookie = True
        except Exception as ce:
            print(f"      [안내] 쿠키 파일 기록 건너뜀: {ce}")

    query = f"ytsearch1:{singer} {song} 가사 audio"
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out_filename,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }
    if has_cookie:
        ydl_opts["cookiefile"] = cookie_path
        print("      └ 보조 계정 쿠키(YT_COOKIES) 인증 적용됨")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(query, download=True)
            if os.path.exists(out_filename) and os.path.getsize(out_filename) > 10000:
                return out_filename
    except Exception as e:
        print(f"      [안내] 신곡 배경음 추출 건너뜀 (나레이션 단독 모드 안전 전환): {e}")
    finally:
        if os.path.exists(cookie_path):
            try:
                os.remove(cookie_path)
            except Exception:
                pass
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

# ================= [VERIFIED: 3중 자켓 백업 + Ken Burns 줌인 + 인기곡/최신곡 분기 쇼츠 엔진] =================
def create_optimized_shorts(singer, song, melon_url, content_html, tab_name="최신곡", output_path="shorts.mp4"):
    is_popular = (tab_name == "인기곡")
    badge_text = "시청자 추천 트로트 명곡" if is_popular else "오늘의 명품 트로트 신곡"
    intro_prefix = "트로트 명곡 추천" if is_popular else "명품 트로트 신곡"

    print(f"   [1/4] Edge-TTS AI 아나운서 음성 생성 중 ({tab_name} 모드)...")
    script_text = f"{intro_prefix}, 가수 {singer}의 {song}! 매일 명곡 소식을 받으시려면 구독을 눌러주세요."
    audio_path = "voice.mp3"
    generate_voice_sync(script_text, audio_path)

    raw_voice_clip = AudioFileClip(audio_path)
    voice_dur = round(max(float(raw_voice_clip.duration) - 0.08, 3.0), 2)
    voice_clip = safe_subclip(raw_voice_clip, 0, voice_dur)

    print("   [2/4] 하이라이트 음원 추출 및 오디오 믹싱 중...")
    bgm_raw_path = fetch_song_highlight_audio(singer, song, "bgm_raw.m4a")
    bgm_clip_full = None

    if bgm_raw_path:
        try:
            bgm_clip_full = AudioFileClip(bgm_raw_path)
            raw_bgm_dur = float(bgm_clip_full.duration)
            target_total = max(voice_dur + 11.5, 16.0)

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
                bgm_part1 = safe_volumex(safe_subclip(bgm_sub, 0, voice_dur), 0.22)
                bgm_part2 = safe_volumex(safe_subclip(bgm_sub, voice_dur, duration), 1.00)
                bgm_ducked = concatenate_audioclips([bgm_part1, bgm_part2])
            else:
                bgm_ducked = safe_volumex(bgm_sub, 0.35)

            bgm_ducked = safe_fadeout(bgm_ducked, 1.5)
            final_audio = CompositeAudioClip([bgm_ducked, safe_start(voice_clip, 0)])
            final_audio = safe_duration(final_audio, duration)
            print(f"      └ 오디오 믹싱 성공! (총 {duration:.1f}초: 멘트 {voice_dur:.1f}초 + 하이라이트 {duration - voice_dur:.1f}초)")
        except Exception as mix_err:
            print(f"      [안내] 오디오 믹싱 예외 발생, 기본 음성으로 전환: {mix_err}")
            duration = voice_dur
            final_audio = CompositeAudioClip([safe_start(voice_clip, 0)])
            final_audio = safe_duration(final_audio, duration)
    else:
        duration = voice_dur
        final_audio = CompositeAudioClip([safe_start(voice_clip, 0)])
        final_audio = safe_duration(final_audio, duration)
        print(f"      └ 기본 나레이션 단독 모드 적용 ({duration:.1f}초)")

    print("   [3/4] 3중 백업 앨범 자켓 확보 및 줌인(Zoom-in) 모션 레이어 구성 중...")
    jacket_img = get_best_album_art(singer, song, melon_url, content_html)

    if jacket_img is None:
        # 최종 폴백: 골드 LP 레코드 스타일 타이포 카드
        jacket_img = Image.new("RGB", (720, 720), color=(24, 30, 52))
        d_temp = ImageDraw.Draw(jacket_img)
        d_temp.ellipse([40, 40, 680, 680], outline=(255, 215, 0), width=6)
        d_temp.ellipse([120, 120, 600, 600], outline=(100, 116, 139), width=3)
        f_temp_singer = get_font_safe(58)
        f_temp_song = get_font_safe(50)
        d_temp.text((360, 310), singer, font=f_temp_singer, fill=(255, 215, 0), anchor="mm")
        d_temp.text((360, 410), song, font=f_temp_song, fill=(255, 255, 255), anchor="mm")

    jacket_img = ImageOps.fit(jacket_img, (720, 720), Image.Resampling.LANCZOS)

    # 1) 배경 레이어 (블러 + 다크 오버레이 + 상/하단 타이포그래피)
    bg_img = jacket_img.resize((1080, 1920), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(radius=45))
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 145))
    bg_img = Image.alpha_composite(bg_img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(bg_img)
    f_badge = get_font_safe(42)
    f_bold = get_font_safe(62)
    f_singer = get_font_safe(54)
    f_cta_main = get_font_safe(40)
    f_cta_sub = get_font_safe(35)

    badge_fill = (185, 28, 28) if is_popular else (217, 119, 6)
    draw.rounded_rectangle([220, 195, 860, 265], radius=35, fill=badge_fill)
    draw.text((540, 230), badge_text, font=f_badge, fill=(255, 255, 255), anchor="mm")

    display_song = song if len(song) <= 15 else song[:14] + "..."
    draw.text((540, 345), display_song, font=f_bold, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 1255), f"가수 : {singer}", font=f_singer, fill=(255, 215, 0), anchor="mm")

    # 하단 구독 유도 + 블로그 링크 이중 배너
    draw.rounded_rectangle([(70, 1525), (1010, 1720)], radius=24, fill=(215, 38, 56))
    draw.text((540, 1585), "▶ 매일 트로트 명곡 알림 → 오른쪽 [구독] 꾹!", font=f_cta_main, fill=(255, 235, 59), anchor="mm")
    draw.text((540, 1660), "◀◀ 전곡·가사는 왼쪽 아래 [채널 사진] 누르고 상단 링크!", font=f_cta_sub, fill=(255, 255, 255), anchor="mm")

    bg_path = "bg_frame.png"
    bg_img.save(bg_path, "PNG")

    # 2) 중앙 자켓 카드 레이어 (정중앙 좌표 고정 후 1.00 -> 1.08 줌인)
    card_with_border = Image.new("RGB", (736, 736), (255, 255, 255))
    card_with_border.paste(jacket_img, (8, 8))
    card_path = "card_frame.png"
    card_with_border.save(card_path, "PNG")

    print(f"   [4/4] MP4 모션 비디오 렌더링 인코딩 중 ({duration}초, Ken Burns 줌인 적용)...")
    bg_clip = safe_duration(ImageClip(bg_path), duration)

    raw_card_clip = safe_duration(ImageClip(card_path), duration)
    base_size = 736.0
    center_y = 455.0 + (base_size / 2.0)

    zoomed_card_clip = safe_resize(raw_card_clip, lambda t: 1.0 + 0.08 * (t / max(duration, 0.1)))
    zoomed_card_clip = safe_position(
        zoomed_card_clip,
        lambda t: (
            int(round((1080.0 - base_size * (1.0 + 0.08 * (t / max(duration, 0.1)))) / 2.0)),
            int(round(center_y - (base_size * (1.0 + 0.08 * (t / max(duration, 0.1)))) / 2.0))
        )
    )

    final_clip = CompositeVideoClip([bg_clip, zoomed_card_clip], size=(1080, 1920))
    final_clip = safe_duration(final_clip, duration)
    final_clip = safe_audio(final_clip, final_audio)
    final_clip.fps = 24
    final_clip.write_videofile(
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
        bg_clip.close()
        raw_card_clip.close()
        final_clip.close()
    except Exception:
        pass

    for temp_f in [audio_path, bg_path, card_path, "bgm_raw.m4a"]:
        if os.path.exists(temp_f):
            os.remove(temp_f)

    return output_path

# ================= [LOCKED: Apps Script 웹훅 업로드] =================
def upload_via_apps_script(file_path, file_name, row_num, new_id, sheet_name="최신곡"):
    with open(file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "action": "upload_shorts",
        "fileName": file_name,
        "fileBase64": b64_data,
        "rowNum": row_num,
        "newId": new_id,
        "sheetName": sheet_name
    }
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=120, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"Apps Script HTTP 오류: {resp.status_code} / {resp.text[:200]}")

    result = resp.json()
    if result.get("status") != "success":
        raise RuntimeError(f"Apps Script 내부 처리 오류: {result.get('message') or result.get('error')}")
    return result.get("fileId")

# ================= [VERIFIED: 저녁 20:30 '인기곡' 교차 발행 + 중복 곡 방지 실행 제어] =================
def run_pipeline():
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    prefix = now_kst.strftime("%m%d")

    # 한국 시간(KST) 저녁 황금시간대(19:00~22:59)에는 '인기곡' 탭 우선 탐색, 대기곡 없으면 '최신곡' 자동 폴백
    if 19 <= now_kst.hour <= 22:
        tab_priority = ["인기곡", "최신곡"]
    else:
        tab_priority = ["최신곡"]

    processed = False
    for tab_name in tab_priority:
        try:
            target_sheet = spreadsheet.worksheet(tab_name)
        except Exception:
            print(f"[안내] '{tab_name}' 시트 탭을 찾을 수 없어 다음 탭으로 넘어갑니다.")
            continue

        rows = target_sheet.get_all_values()
        print(f"[시작] '{tab_name}' 시트에서 작업 대상을 탐색합니다... (KST {now_kst.strftime('%H:%M')})")

        current_numbers = []
        already_processed_songs = set()

        for r in rows:
            val = str(r[1]).strip() if len(r) > 1 else ""
            if val.startswith(prefix + "-"):
                try:
                    num = int(val.split("-")[1])
                    current_numbers.append(num)
                except Exception:
                    pass
            # 이미 쇼츠 ID가 발급된 (가수, 곡명) 쌍 수집하여 중복 발행 방지
            s_name = r[2].strip() if len(r) > 2 else ""
            t_name = r[3].strip() if len(r) > 3 else ""
            s_id = r[15].strip() if len(r) > 15 else ""
            if s_name and t_name and s_id:
                already_processed_songs.add((s_name, t_name))

        next_seq = (max(current_numbers) + 1) if current_numbers else 1
        new_id = f"{prefix}-{next_seq}"

        for idx in range(3, len(rows)):
            row_num = idx + 1
            singer = rows[idx][2].strip() if len(rows[idx]) > 2 else ""
            song = rows[idx][3].strip() if len(rows[idx]) > 3 else ""
            melon_url = rows[idx][4].strip() if len(rows[idx]) > 4 else ""
            status = rows[idx][7].strip() if len(rows[idx]) > 7 else ""
            shorts_id = rows[idx][15].strip() if len(rows[idx]) > 15 else ""
            content_html = rows[idx][6] if len(rows[idx]) > 6 else ""

            if status == "Published" and not shorts_id:
                if (singer, song) in already_processed_songs:
                    print(f"[중복 스킵] [{tab_name}] {row_num}행: {singer} - {song} (이미 발행된 곡)")
                    continue

                processed = True
                print(f"\n[대상 포착] [{tab_name}] {row_num}행: {singer} - {song} (발행 ID: {new_id})")

                clean_singer = "".join(c for c in singer if c.isalnum() or c in (" ", "_")).strip()
                clean_song = "".join(c for c in song if c.isalnum() or c in (" ", "_")).strip()
                video_filename = f"Shorts_{new_id}_{clean_singer}_{clean_song}.mp4"

                try:
                    rendered = create_optimized_shorts(singer, song, melon_url, content_html, tab_name, video_filename)
                    print(f"   [전송] 대표님 Google Drive(Shorts_Auto) 및 [{tab_name}] 시트 동기화 요청 중...")
                    drive_id = upload_via_apps_script(rendered, video_filename, row_num, new_id, tab_name)

                    print(f"\n[성공] [{tab_name}] {row_num}행: ID [{new_id}] / Drive ID [{drive_id}] / 'Shorts_Ready' 확정 완료!")
                    if os.path.exists(rendered):
                        os.remove(rendered)
                    return

                except Exception as e:
                    print(f"[전송/렌더링 실패] {str(e)}")
                    return

    if not processed:
        print("[안내] 현재 처리 대기 중인 대상(Published & File ID 공란)이 없습니다.")

if __name__ == "__main__":
    run_pipeline()
