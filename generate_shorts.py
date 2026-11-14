import os
import re
import json
import math
import textwrap
import asyncio
import datetime
import numpy as np
import requests
from bs4 import BeautifulSoup
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from moviepy.editor import VideoClip, AudioFileClip, CompositeAudioClip
from moviepy.audio.AudioClip import AudioArrayClip
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# 1. 서비스 계정 무인 인증
sa_info = json.loads(os.environ["GCP_SA_KEY"])
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(creds)

SPREADSHEET_NAME = "Copy of 블로그 자동화 관리 시트"
sheet = gc.open(SPREADSHEET_NAME).worksheet("최신곡")

# 2. 가수명/곡명 정제 (괄호 속 한자·영문 제거로 폰트 깨짐 및 TTS 중복 방지)
def clean_display_name(text):
    cleaned = re.sub(r'\s*[\(\[\{][^\)\]\}]*[\)\]\}]', '', str(text)).strip()
    return cleaned if cleaned else str(text).strip()

# 3. 멜론 고화질 공식 앨범 자켓 크롤러
def get_melon_album_art(melon_song_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        if not melon_song_url or not melon_song_url.startswith("http"):
            return None
        res = requests.get(melon_song_url, headers=headers, timeout=7)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                img_url = meta_img['content']
                img_url = re.sub(r'resize/\d+', 'resize/500', img_url)
                return img_url
    except Exception as e:
        print(f"[안내] 멜론 크롤링 예외: {e}")
    return None

# 4. 블로그 본문 HTML(G열)에서 감성 핵심 가사 2줄 자동 추출
def extract_highlight_lyrics(content_html, singer, song):
    fallback_lines = [
        f"가슴 깊이 스며드는 {singer}의 명품 보이스,",
        f"오늘 하루 지친 마음을 위로하는 '{song}'"
    ]
    if not content_html:
        return fallback_lines

    try:
        soup = BeautifulSoup(content_html, 'html.parser')
        for tag in soup(['script', 'style', 'h1', 'h2', 'h3', 'table', 'a']):
            tag.decompose()

        raw_text = soup.get_text(separator="\n")
        lines = [re.sub(r'\s+', ' ', ln).strip() for ln in raw_text.split("\n")]

        # 가사다운 감성 줄 필터링 (8~26자 사이의 한글 중심 문장)
        blacklist = ("발매", "앨범", "작사", "작곡", "편곡", "멜론", "유튜브", "구독", "좋아요", "가사", "전문", "감상", "http", "www", "tistory", "출처")
        candidates = []
        for ln in lines:
            if 8 <= len(ln) <= 26 and not any(b in ln for b in blacklist):
                if re.search(r'[가-힣]{4,}', ln):
                    candidates.append(ln)

        if len(candidates) >= 2:
            # 도입부보다 감정이 고조되는 중반부 2줄 우선 선택
            mid = min(len(candidates) // 3, len(candidates) - 2)
            return [candidates[mid], candidates[mid + 1]]
        elif len(candidates) == 1:
            return [candidates[0], fallback_lines[1]]
    except Exception:
        pass

    return fallback_lines

# 5. 저작권 0% 감성 트로트 피아노/패드 배경 BGM 실시간 합성기
def generate_ambient_bgm(duration, sample_rate=44100):
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    # 감성적인 Am - F - C - G 코드 진행 (각 3.5초 순환)
    chords = [
        [220.00, 261.63, 329.63, 440.00],  # Am
        [174.61, 220.00, 261.63, 349.23],  # F
        [130.81, 261.63, 329.63, 392.00],  # C
        [196.00, 246.94, 293.66, 392.00],  # G
    ]
    wave = np.zeros_like(t)
    chord_len = 3.5

    for idx, freqs in enumerate(chords):
        # 전체 길이에 걸쳐 14초 주기로 반복
        cycle_pos = t % (chord_len * 4)
        mask = (cycle_pos >= idx * chord_len) & (cycle_pos < (idx + 1) * chord_len)
        local_t = cycle_pos[mask] - (idx * chord_len)
        # 부드러운 어택/릴리즈 엔벨로프
        env = np.sin(np.pi * np.clip(local_t / chord_len, 0, 1)) ** 0.7

        chord_sig = np.zeros_like(local_t)
        for f in freqs:
            chord_sig += 0.25 * np.sin(2 * np.pi * f * local_t)
            chord_sig += 0.08 * np.sin(2 * np.pi * (f * 2) * local_t)  # 따뜻한 배음
        wave[mask] = chord_sig * env

    # 전체 페이드 인/아웃 및 은은한 볼륨(16%) 적용
    fade_samples = int(sample_rate * 0.8)
    if len(wave) > fade_samples * 2:
        wave[:fade_samples] *= np.linspace(0, 1, fade_samples)
        wave[-fade_samples:] *= np.linspace(1, 0, fade_samples)

    wave = (wave * 0.16).astype(np.float32)
    stereo = np.column_stack((wave, wave))
    return AudioArrayClip(stereo, fps=sample_rate)

# 6. Edge-TTS 감성 후킹 나레이션 생성
async def _async_generate_voice(text, filename="voice.mp3"):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural", rate="+4%", pitch="-1Hz")
    await communicate.save(filename)

def generate_voice_sync(text, filename="voice.mp3"):
    asyncio.run(_async_generate_voice(text, filename))

# 7. 고체류시간 쇼츠 비디오 합성 엔진 (줌인 모션 + 가사 카드 + 진행바 + BGM)
def create_optimized_shorts(singer, song, album_url, lyrics_lines, output_path="shorts.mp4"):
    print(f"   [1/3] 감성 후킹 AI 음성 및 배경 BGM 생성 중...")
    script_text = (
        f"가슴을 울리는 명품 트로트, {singer}의 {song}. "
        f"{lyrics_lines[0]} {lyrics_lines[1]}. "
        f"전체 노래와 가사는 왼쪽 아래 동그란 채널 사진을 눌러 감상해 보세요."
    )
    audio_path = "voice.mp3"
    generate_voice_sync(script_text, audio_path)

    voice_clip = AudioFileClip(audio_path)
    duration = voice_clip.duration + 1.5
    bgm_clip = generate_ambient_bgm(duration)
    final_audio = CompositeAudioClip([bgm_clip, voice_clip.volumex(1.15)])

    print(f"   [2/3] 공식 앨범 자켓, 가사 자막 및 모션 프레임 구성 중...")
    jacket_img = None
    if album_url and album_url.startswith("http"):
        try:
            res = requests.get(album_url, timeout=7)
            jacket_img = Image.open(BytesIO(res.content)).convert("RGB")
        except Exception:
            jacket_img = None

    font_path = "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf"

    if jacket_img is None:
        jacket_img = Image.new("RGB", (800, 800), color=(45, 35, 60))
        d_temp = ImageDraw.Draw(jacket_img)
        f_temp = ImageFont.truetype(font_path, 55)
        d_temp.text((400, 350), singer, font=f_temp, fill=(255, 255, 255), anchor="mm")
        d_temp.text((400, 440), song, font=f_temp, fill=(255, 215, 0), anchor="mm")

    jacket_base = jacket_img.resize((820, 820), Image.Resampling.LANCZOS)

    # 블러 배경 1회 사전 생성 (렌더링 속도 최적화)
    bg_base = jacket_base.resize((1080, 1920), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(radius=45))
    overlay = Image.new("RGBA", (1080, 1920), (10, 10, 18, 155))
    bg_base = Image.alpha_composite(bg_base.convert("RGBA"), overlay).convert("RGB")

    # 텍스트/배너 오버레이 레이어 1회 사전 생성
    ui_layer = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ui_layer)

    f_tag = ImageFont.truetype(font_path, 38)
    f_title = ImageFont.truetype(font_path, 58)
    f_singer = ImageFont.truetype(font_path, 48)
    f_lyric = ImageFont.truetype(font_path, 40)
    f_guide_top = ImageFont.truetype(font_path, 34)
    f_guide_btm = ImageFont.truetype(font_path, 42)

    # 상단 감성 배지 & 곡 제목
    draw.rounded_rectangle([(310, 130), (770, 195)], radius=30, fill=(255, 215, 0, 230))
    draw.text((540, 162), "♪ 오늘의 감성 트로트 추천", font=f_tag, fill=(20, 20, 25, 255), anchor="mm")

    wrapped_song = textwrap.wrap(song, width=14)[:2]
    for i, line in enumerate(wrapped_song):
        y_pos = 255 + (i * 70) if len(wrapped_song) > 1 else 285
        draw.text((540, y_pos), line, font=f_title, fill=(255, 255, 255, 255), anchor="mm")

    # 자켓 액자 테두리 (640x640 영역: x=220..860, y=410..1050)
    draw.rectangle([(214, 404), (866, 1056)], outline=(255, 215, 0, 220), width=5)

    # 가수명 표시 (한자 제거된 깔끔한 한글명)
    draw.text((540, 1115), f"노래 : {singer}", font=f_singer, fill=(255, 235, 130, 255), anchor="mm")

    # 핵심 가사 하이라이트 카드 (y=1175..1375)
    draw.rounded_rectangle([(70, 1175), (1010, 1375)], radius=24, fill=(0, 0, 0, 175), outline=(255, 255, 255, 90), width=2)
    draw.text((540, 1235), f"“ {lyrics_lines[0]} ”", font=f_lyric, fill=(255, 255, 255, 255), anchor="mm")
    draw.text((540, 1310), f"“ {lyrics_lines[1]} ”", font=f_lyric, fill=(255, 240, 170, 255), anchor="mm")

    # 하단 구독 & 프로필 동선 유도 배너 (y=1490..1650)
    draw.rounded_rectangle([(70, 1490), (1010, 1650)], radius=24, fill=(215, 38, 56, 245))
    draw.text((540, 1540), "📌 가사 전문 & 전곡 듣기 + 구독하기", font=f_guide_top, fill=(255, 255, 255, 255), anchor="mm")
    draw.text((540, 1600), "👈 왼쪽 아래 [동그란 사진] 누르면 바로 연결!", font=f_guide_btm, fill=(255, 235, 59, 255), anchor="mm")

    # 프레임별 Ken Burns 줌인 + 실시간 재생 진행바 렌더링 함수
    def make_frame(t):
        progress = min(max(t / duration, 0.0), 1.0)
        # 1.0배 -> 1.14배 부드러운 줌인
        zoom = 1.0 + 0.14 * progress
        crop_size = int(820 / zoom)
        offset = (820 - crop_size) // 2
        cropped = jacket_base.crop((offset, offset, offset + crop_size, offset + crop_size))
        zoomed_jacket = cropped.resize((640, 640), Image.Resampling.BILINEAR)

        frame = bg_base.copy()
        frame.paste(zoomed_jacket, (220, 410))
        frame = Image.alpha_composite(frame.convert("RGBA"), ui_layer).convert("RGB")

        # 실시간 하단 진행 게이지 바 (y=1415..1433)
        f_draw = ImageDraw.Draw(frame)
        f_draw.rounded_rectangle([(120, 1415), (960, 1433)], radius=9, fill=(70, 70, 85))
        bar_end = 120 + int(840 * progress)
        if bar_end > 128:
            f_draw.rounded_rectangle([(120, 1415), (bar_end, 1433)], radius=9, fill=(255, 215, 0))

        return np.array(frame)

    print(f"   [3/3] MP4 모션 비디오 렌더링 인코딩 중 ({duration:.1f}초)...")
    video_clip = VideoClip(make_frame, duration=duration).set_audio(final_audio)
    video_clip.write_videofile(
        output_path,
        fps=15,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        verbose=False,
        logger=None
    )

    voice_clip.close()
    bgm_clip.close()
    final_audio.close()
    video_clip.close()
    if os.path.exists(audio_path):
        os.remove(audio_path)
    return output_path

# 8. GitHub Releases 무제한 저장소에 MP4 업로드
def upload_to_github_release(file_path, file_name, tag_name="shorts-media"):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    rel_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag_name}"
    res = requests.get(rel_url, headers=headers)
    if res.status_code == 200:
        release_data = res.json()
    else:
        create_res = requests.post(
            f"https://api.github.com/repos/{repo}/releases",
            headers=headers,
            json={
                "tag_name": tag_name,
                "target_commitish": "main",
                "name": "Generated Shorts Media",
                "draft": False,
                "prerelease": False
            }
        )
        create_res.raise_for_status()
        release_data = create_res.json()

    upload_url = release_data["upload_url"].split("{")[0]

    for asset in release_data.get("assets", []):
        if asset["name"] == file_name:
            requests.delete(asset["url"], headers=headers)

    with open(file_path, "rb") as f:
        upload_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "video/mp4"
        }
        up_res = requests.post(
            f"{upload_url}?name={requests.utils.quote(file_name)}",
            headers=upload_headers,
            data=f
        )
        up_res.raise_for_status()
        return up_res.json()["browser_download_url"]

# 9. 메인 실행 로직
def run_pipeline():
    rows = sheet.get_all_values()
    print("[시작] '최신곡' 시트에서 쇼츠 제작 대상을 탐색합니다...")

    kst = datetime.timezone(datetime.timedelta(hours=9))
    prefix = datetime.datetime.now(kst).strftime("%m%d")

    current_numbers = []
    for r in rows:
        val = str(r[1]).strip()
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
        raw_singer = rows[idx][2].strip()
        raw_song = rows[idx][3].strip()
        melon_url = rows[idx][4].strip()
        status = rows[idx][7].strip()
        shorts_id = rows[idx][15].strip() if len(rows[idx]) > 15 else ""
        content_html = rows[idx][6] if len(rows[idx]) > 6 else ""

        if status in ("Published", "Shorts_Render_Error") and not shorts_id:
            processed = True
            singer = clean_display_name(raw_singer)
            song = clean_display_name(raw_song)
            print(f"🎯 [대상 포착] {row_num}행: {singer} - {song} (ID: {new_id})")

            jacket_url = get_melon_album_art(melon_url)
            if not jacket_url:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
                jacket_url = m.group(1) if m else None

            lyrics_lines = extract_highlight_lyrics(content_html, singer, song)

            safe_id = re.sub(r'[^0-9A-Za-z\-]', '', new_id)
            video_filename = f"Shorts_{safe_id}.mp4"

            try:
                rendered = create_optimized_shorts(singer, song, jacket_url, lyrics_lines, video_filename)
                media_url = upload_to_github_release(rendered, video_filename)

                sheet.update_cell(row_num, 2, new_id)
                sheet.update_cell(row_num, 16, media_url)
                sheet.update_cell(row_num, 8, "Shorts_Ready")

                print(f"🎉 [성공] {row_num}행: ID [{new_id}] 부여 및 'Shorts_Ready' 확정 완료!")
                print(f"   ▶ 추출된 가사 자막: {lyrics_lines[0]} / {lyrics_lines[1]}")
                print(f"   ▶ 저장된 영상 주소: {media_url}")
                if os.path.exists(rendered):
                    os.remove(rendered)
                break

            except Exception as e:
                print(f"❌ 렌더링 실패: {str(e)}")
                sheet.update_cell(row_num, 8, "Shorts_Render_Error")
                break

    if not processed:
        print("[안내] 현재 처리 대기 중인 대상이 없습니다.")

if __name__ == "__main__":
    run_pipeline()
