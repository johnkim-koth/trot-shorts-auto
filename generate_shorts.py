import os
import re
import json
import textwrap
import asyncio
import datetime
import requests
from bs4 import BeautifulSoup
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from moviepy.editor import ImageClip, AudioFileClip
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# 1. 서비스 계정 무인 인증 (시트 이름 검색을 위해 drive + spreadsheets 스코프 필수 포함)
sa_info = json.loads(os.environ["GCP_SA_KEY"])
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
gc = gspread.authorize(creds)

SPREADSHEET_NAME = "Copy of 블로그 자동화 관리 시트"
sheet = gc.open(SPREADSHEET_NAME).worksheet("최신곡")

# 2. 멜론 실제 고화질 공식 앨범 자켓 크롤러
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

# 3. Edge-TTS 고품질 나레이션 생성
async def _async_generate_voice(text, filename="voice.mp3"):
    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural", rate="+5%")
    await communicate.save(filename)

def generate_voice_sync(text, filename="voice.mp3"):
    asyncio.run(_async_generate_voice(text, filename))

# 4. 5060 최적화 숏츠 비디오 합성 엔진
def create_optimized_shorts(singer, song, album_url, output_path="shorts.mp4"):
    print(f"   [1/3] Edge-TTS AI 음성 생성 중...")
    script_text = f"오늘 전해드릴 명품 트로트 신곡은 가수 {singer}의 {song}입니다. 전체 노래와 가사는 왼쪽 아래 동그란 사진을 눌러보세요."
    audio_path = "voice.mp3"
    generate_voice_sync(script_text, audio_path)
    
    audio_clip = AudioFileClip(audio_path)
    duration = audio_clip.duration + 1.2

    print(f"   [2/3] 공식 앨범 자켓 및 5060 맞춤 화면 구성 중...")
    jacket_img = None
    if album_url and album_url.startswith("http"):
        try:
            res = requests.get(album_url, timeout=7)
            jacket_img = Image.open(BytesIO(res.content)).convert("RGB")
        except Exception:
            jacket_img = None

    font_path = "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf"

    if jacket_img is None:
        jacket_img = Image.new("RGB", (720, 720), color=(45, 35, 60))
        d_temp = ImageDraw.Draw(jacket_img)
        f_temp = ImageFont.truetype(font_path, 55)
        d_temp.text((360, 320), singer, font=f_temp, fill=(255, 255, 255), anchor="mm")
        d_temp.text((360, 400), song, font=f_temp, fill=(255, 215, 0), anchor="mm")

    jacket_img = jacket_img.resize((720, 720), Image.Resampling.LANCZOS)

    bg_img = jacket_img.resize((1080, 1920), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(radius=45))
    overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 135))
    bg_img = Image.alpha_composite(bg_img.convert("RGBA"), overlay).convert("RGB")

    border = Image.new("RGB", (728, 728), (255, 255, 255))
    border.paste(jacket_img, (4, 4))
    bg_img.paste(border, (176, 480))

    draw = ImageDraw.Draw(bg_img)
    f_bold = ImageFont.truetype(font_path, 58)
    f_sub = ImageFont.truetype(font_path, 44)
    f_guide = ImageFont.truetype(font_path, 36)

    draw.text((540, 210), "[ 신곡 발매 안내 ]", font=f_sub, fill=(255, 215, 0), anchor="mm")

    wrapped_song = textwrap.wrap(song, width=14)[:2]
    for i, line in enumerate(wrapped_song):
        y_pos = 300 + (i * 72) if len(wrapped_song) > 1 else 330
        draw.text((540, y_pos), line, font=f_bold, fill=(255, 255, 255), anchor="mm")

    draw.text((540, 1270), f"가수: {singer}", font=f_bold, fill=(255, 255, 255), anchor="mm")

    draw.rectangle([(80, 1560), (1000, 1710)], fill=(215, 38, 56))
    draw.text((540, 1610), "📌 전체 노래 & 가사 전문 감상실", font=f_guide, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 1665), "👈 왼쪽 아래 [동그란 사진] 누르면 바로 연결!", font=f_sub, fill=(255, 235, 59), anchor="mm")

    frame_path = "frame.png"
    bg_img.save(frame_path)

    print(f"   [3/3] MP4 비디오 렌더링 인코딩 중...")
    clip = ImageClip(frame_path).set_duration(duration).set_audio(audio_clip)
    clip.fps = 24
    clip.write_videofile(output_path, codec="libx264", audio_codec="aac", verbose=False, logger=None)

    audio_clip.close()
    clip.close()
    if os.path.exists(audio_path): os.remove(audio_path)
    if os.path.exists(frame_path): os.remove(frame_path)
    return output_path

# 5. GitHub Releases 무제한 저장소에 MP4 업로드 & 다운로드 URL 반환
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

# 6. 메인 실행 로직
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
            except:
                pass
    next_seq = (max(current_numbers) + 1) if current_numbers else 1
    new_id = f"{prefix}-{next_seq}"

    processed = False
    for idx in range(3, len(rows)):
        row_num = idx + 1
        singer = rows[idx][2].strip()
        song = rows[idx][3].strip()
        melon_url = rows[idx][4].strip()
        status = rows[idx][7].strip()
        shorts_id = rows[idx][15].strip() if len(rows[idx]) > 15 else ""
        content_html = rows[idx][6] if len(rows[idx]) > 6 else ""

        if status in ("Published", "Shorts_Render_Error") and not shorts_id:
            processed = True
            print(f"🎯 [대상 포착] {row_num}행: {singer} - {song} (ID: {new_id})")

            jacket_url = get_melon_album_art(melon_url)
            if not jacket_url:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
                jacket_url = m.group(1) if m else None

            clean_singer = "".join(c for c in singer if c.isalnum() or c in (' ', '_')).strip()
            clean_song = "".join(c for c in song if c.isalnum() or c in (' ', '_')).strip()
            video_filename = f"Shorts_{new_id}_{clean_singer}_{clean_song}.mp4"

            try:
                rendered = create_optimized_shorts(singer, song, jacket_url, video_filename)
                media_url = upload_to_github_release(rendered, video_filename)

                sheet.update_cell(row_num, 2, new_id)
                sheet.update_cell(row_num, 16, media_url)
                sheet.update_cell(row_num, 8, "Shorts_Ready")

                print(f"🎉 [성공] {row_num}행: ID [{new_id}] 부여 및 'Shorts_Ready' 확정 완료!")
                print(f"   ▶ 저장된 영상 주소: {media_url}")
                if os.path.exists(rendered): os.remove(rendered)
                break

            except Exception as e:
                print(f"❌ 렌더링 실패: {str(e)}")
                sheet.update_cell(row_num, 8, "Shorts_Render_Error")
                break

    if not processed:
        print("[안내] 현재 처리 대기 중인 대상이 없습니다.")

if __name__ == "__main__":
    run_pipeline()
