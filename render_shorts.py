/**
 * [YouTube Shorts 100% 무인 자동 발행 엔진 - 티스토리 발행 완료(Published -> Shorts_Ready) 기준 연동]
 */
function uploadShortsFromSheet() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    Logger.log("[안내] 다른 프로세스가 이미 실행 중입니다.");
    return;
  }

  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const targetTabNames = ["인기곡", "최신곡", "신인곡"];
    const defaultBlogUrl = "https://trotmusic.tistory.com";

    for (let t = 0; t < targetTabNames.length; t++) {
      const tabName = targetTabNames[t];
      const sheet = ss.getSheetByName(tabName);
      if (!sheet) continue;

      const isPopular = (tabName === "인기곡");
      const data = sheet.getDataRange().getValues();

      for (let i = 3; i < data.length; i++) {
        const rowNum = i + 1;

        const rawSinger = String(data[i][2] || "").replace(/\u00A0/g, " ").trim();
        const rawSong = String(data[i][3] || "").replace(/\u00A0/g, " ").trim();
        const rawContentHtml = String(data[i][6] || "");
        const status = String(data[i][7] || "").trim();
        const rawPostUrl = String(data[i][9] || "").trim();
        const rawFileField = String(data[i][15] || "").trim();
        const existingShortsLink = String(data[i][16] || "").trim();

        // 티스토리 발행 후 render_shorts.py가 영상을 만들어 'Shorts_Ready'로 넘긴 행만 업로드
        const isTarget = (status === "Shorts_Ready") && rawFileField !== "" && existingShortsLink === "";

        if (isTarget) {
          if (!rawSinger || !rawSong) continue;

          const isDirectUrl = isValidHttpUrl(rawFileField);
          const fileId = isDirectUrl ? "" : extractCleanDriveId(rawFileField);
          if (!isDirectUrl && !fileId) {
            sheet.getRange(rowNum, 8).setValue("Shorts_Error");
            SpreadsheetApp.flush();
            return;
          }

          const singer = cleanBracketName(rawSinger);
          const song = cleanBracketName(rawSong);

          sheet.getRange(rowNum, 8).setValue("Shorts_Processing");
          SpreadsheetApp.flush();

          try {
            let videoBlob = null;
            let fileSizeBytes = 0;
            let displayFileName = "";

            if (isDirectUrl) {
              const fetchOptions = { muteHttpExceptions: true, followRedirects: true };
              const ghToken = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
              if (ghToken) fetchOptions.headers = { "Authorization": "Bearer " + ghToken };
              const response = UrlFetchApp.fetch(rawFileField, fetchOptions);
              if (response.getResponseCode() !== 200) {
                throw new Error(`영상 URL 다운로드 실패 (HTTP ${response.getResponseCode()})`);
              }
              videoBlob = response.getBlob();
              displayFileName = decodeURIComponent(rawFileField.split("/").pop() || `Shorts_${rowNum}.mp4`);
              videoBlob.setName(displayFileName);
              videoBlob.setContentType("video/mp4");
              fileSizeBytes = videoBlob.getBytes().length;
            } else {
              const file = DriveApp.getFileById(fileId);
              fileSizeBytes = file.getSize();
              displayFileName = file.getName();
              videoBlob = file.getBlob();
            }

            if (fileSizeBytes > 50 * 1024 * 1024) {
              sheet.getRange(rowNum, 8).setValue("Shorts_FileTooLarge");
              SpreadsheetApp.flush();
              return;
            }

            const targetUrl = isValidHttpUrl(rawPostUrl) ? rawPostUrl : defaultBlogUrl;
            const blogTagUrl = `https://trotmusic.tistory.com/tag/${encodeURIComponent(singer)}`;
            const cleanSinger = singer.replace(/,/g, " ").trim();
            const safeSingerTag = cleanSinger.replace(/[^가-힣a-zA-Z0-9]/g, "");
            const safeSongTag = song.replace(/[^가-힣a-zA-Z0-9]/g, "");

            const titleHook = buildEmotionalTitleHook(song, rawContentHtml, isPopular);
            let cleanTitle = `${cleanSinger} - ${song} | ${titleHook} #트로트 #Shorts`;
            if (cleanTitle.length > 95) {
              cleanTitle = `${cleanSinger} - ${song} | 명품 트로트 #트로트 #Shorts`.slice(0, 95);
            }

            const descriptionText =
              `▶ 매일 아침·점심·저녁 명품 트로트 신곡 소식 받으시려면 [구독]을 눌러주세요!\n\n` +
              `가수 : ${cleanSinger}\n` +
              `곡명 : ${song}\n\n` +
              `▼ [${cleanSinger} - ${song}] 티스토리 전체 가사 / 작사·작곡 정보 보기 ▼\n` +
              `${targetUrl}\n` +
              `▶ ${cleanSinger} 다른 명곡 모아듣기: ${blogTagUrl}\n\n` +
              `💡 [모바일 시청 안내] 왼쪽 아래 채널 프로필 사진을 누르신 뒤 상단 링크를 터치하시면 전체 노래와 가사를 바로 보실 수 있습니다.\n\n` +
              `#Shorts #트로트 #${safeSingerTag} #${safeSongTag} #트로트신곡 #트로트가사 #성인가요`;

            const videoResource = {
              snippet: {
                title: cleanTitle,
                description: descriptionText,
                tags: [cleanSinger, song, `${cleanSinger} 노래`, "트로트", "Shorts", "쇼츠", "트로트신곡", "트로트명곡", "트로트가사", "성인가요"].filter(Boolean),
                categoryId: "10"
              },
              status: {
                privacyStatus: "public",
                selfDeclaredMadeForKids: false
              }
            };

            const uploadedVideo = YouTube.Videos.insert(videoResource, 'snippet,status', videoBlob);
            const videoId = uploadedVideo.id;
            const shortsUrl = `https://www.youtube.com/shorts/${videoId}`;

            sheet.getRange(rowNum, 8).setValue("Shorts_Done");
            sheet.getRange(rowNum, 17).setValue(shortsUrl);
            SpreadsheetApp.flush();

            waitForVideoReady(videoId, 4);

            const commentBody =
              `🎵 [${cleanSinger} - ${song}] 티스토리 가사 전문 & 풀버전 감상하기\n` +
              `👉 왼쪽 아래 채널 프로필(동그란 사진) 클릭 ➡️ 프로필 상단 링크(trotmusic)를 누르시면 전체 가사와 노래를 바로 감상하실 수 있습니다!\n` +
              `❤️ [구독]을 눌러주시면 매일 가슴 따뜻한 트로트 명곡을 전해드립니다.`;

            const fallbackBody =
              `🎵 [${cleanSinger} - ${song}] 전체 가사 및 앨범 정보는 상단 채널 프로필 링크를 확인해 주세요! 구독과 좋아요 감사합니다.`;

            postCommentStrict(videoId, commentBody, fallbackBody);
            Logger.log(`[발행 완료] [${tabName}] ${rowNum}행: ${shortsUrl}`);
            return;

          } catch (innerErr) {
            Logger.log(`[오류] [${tabName}] ${rowNum}행: ${innerErr.message}`);
            sheet.getRange(rowNum, 8).setValue("Shorts_Error");
            SpreadsheetApp.flush();
            return;
          }
        }
      }
    }

    Logger.log("[안내] 현재 업로드 대기 중인 'Shorts_Ready' 행이 없습니다.");
  } finally {
    lock.releaseLock();
  }
}

function buildEmotionalTitleHook(song, rawContentHtml, isPopular) {
  const plainText = String(rawContentHtml || "").replace(/<[^>]+>/g, " ");
  const searchPool = `${song} ${plainText}`;

  const hookRules = [
    { keys: ["눈물", "사부곡", "아버지", "어머니", "부모", "그리움"], label: "가슴 뭉클한 사연 명곡" },
    { keys: ["애절", "이별", "눈시울", "슬픈", "그리운", "발라드"], label: "가슴 먹먹한 감성 트로트" },
    { keys: ["흥겨운", "신나는", "경쾌한", "댄스", "잔치", "에너지"], label: "어깨가 들썩이는 흥겨운 트로트" },
    { keys: ["위로", "따뜻한", "희망", "인생", "세월", "추억"], label: "마음을 울리는 인생 명곡" },
    { keys: ["사랑", "연인", "순정", "고백", "설렘"], label: "깊은 울림의 사랑 노래" }
  ];

  for (let r = 0; r < hookRules.length; r++) {
    const rule = hookRules[r];
    for (let k = 0; k < rule.keys.length; k++) {
      if (searchPool.indexOf(rule.keys[k]) !== -1) return rule.label;
    }
  }
  return isPopular ? "시청자 추천 명품 트로트" : "오늘 꼭 들어야 할 명품 트로트";
}

function cleanBracketName(input) {
  if (!input) return "";
  const cleaned = input.replace(/\s*[\(\[\{][^\)\]\}]*[\)\]\}]$/g, "").replace(/\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*/g, " ").trim();
  return cleaned || input.trim();
}

function extractCleanDriveId(input) {
  if (!input) return "";
  const match = input.match(/[-\w]{25,}/);
  return match ? match[0] : "";
}

function isValidHttpUrl(string) {
  if (!string) return false;
  return string.startsWith("http://") || string.startsWith("https://");
}

function waitForVideoReady(videoId, maxRetries) {
  for (let i = 0; i < maxRetries; i++) {
    Utilities.sleep(5000);
    try {
      const res = YouTube.Videos.list('id,status', { id: videoId });
      if (res && res.items && res.items.length > 0) {
        Utilities.sleep(2000);
        return true;
      }
    } catch (e) {}
  }
  return false;
}

function postCommentStrict(videoId, mainText, altText) {
  try {
    YouTube.CommentThreads.insert({ snippet: { videoId: videoId, topLevelComment: { snippet: { textOriginal: mainText } } } }, 'snippet');
    return true;
  } catch (err1) {
    Utilities.sleep(3000);
    try {
      YouTube.CommentThreads.insert({ snippet: { videoId: videoId, topLevelComment: { snippet: { textOriginal: altText } } } }, 'snippet');
      return true;
    } catch (err2) {
      return false;
    }
  }
}
