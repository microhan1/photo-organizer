# 사진 정리 (Photo Organizer)

<img src="assets/icon.png" width="96" alt="icon">

[English](README.en.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md)

> **인터넷 없이 동작합니다.**
> **사진은 어디에도 올라가지 않습니다.**
> **되돌리기가 있습니다.**

폰·카메라·카카오톡에서 쏟아진 사진과 동영상을 촬영 날짜별 폴더(`2024/2024-03-15/`)로 정리하고, 아이폰 HEIC를 JPG로 바꾸고, 같은 사진을 걸러내는 Windows 프로그램입니다.

![정리 전과 후의 폴더](docs/before_after.png)

![프로그램 화면](docs/screenshot.png)

## 다운로드

- **실행 파일**: [Releases](https://github.com/microhan1/photo-organizer/releases)에서 `photo-organizer.exe`를 받아 더블클릭. 설치가 필요 없습니다. (서명되지 않은 exe라 SmartScreen 경고가 뜨면 "추가 정보 → 실행")
- **소스 실행** (Python 3.12):

```bash
pip install -r requirements.txt
python main.py
```

## 사용법

1. 사진 폴더를 창에 끌어다 놓습니다.
2. 표에서 촬영일·근거·새 경로를 확인합니다. 날짜가 틀린 사진은 촬영일 칸을 두 번 눌러 고칩니다(폴더만 바뀌고 파일 속 EXIF는 그대로).
3. **실행**을 누릅니다. 마음에 안 들면 **되돌리기**.

실행 전에는 아무것도 바뀌지 않습니다. 기록(`organize_log.json`)이 먼저 저장되고, 저장할 수 없으면 아무 파일도 옮기지 않습니다. 되돌리기는 옮긴 파일, 변환으로 만든 JPG, 지운 빈 폴더를 모두 원래대로 돌려놓습니다.

| 상태 | 뜻 |
| --- | --- |
| 정상 | 새 경로로 옮김 |
| 변환 | 옮기면서 HEIC → JPG |
| 날짜 없음 | 날짜를 찾지 못함. 기본은 제자리(설정에서 "날짜 없음" 폴더로) |
| 중복 | `_중복` 폴더로 보냄(삭제하지 않음) |
| 충돌 | 같은 이름이 있어 ` (2)`를 붙임 |
| 변경 없음 | 이미 제자리. 다시 실행하면 전부 이것 |

## 날짜를 어디서 읽나

위에서부터 처음 찾은 것을 씁니다. 표의 "근거" 칸에 어디서 읽었는지 나옵니다.

| 순서 | 출처 | 비고 |
| --- | --- | --- |
| 1 | 촬영 정보 EXIF `DateTimeOriginal` | JPG·HEIC·TIFF·RAW(DNG·CR2·NEF·ARW)·PNG. 찍은 곳의 시각 그대로(시간대 변환 없음) |
| 2 | EXIF `DateTimeDigitized`, `DateTime` | |
| 3 | 동영상 정보 | MP4·MOV·3GP. 아이폰 동영상은 로컬 시각(`creationdate`)을 먼저 |
| 4 | 파일명 | 아래 표 |
| 5 | 짝 파일 | HEIC↔JPG, RAW↔JPG, 사진↔라이브 포토 `.mov` |
| 6 | 파일 수정 시각 | 옵션, 기본 끔. 근거에 "수정 시각 (불확실)" |

카메라 시계가 초기화된 날짜(2000-01-01 00:00 등)는 "촬영 정보 (의심)"으로 표시하고, 파일명에 날짜가 있으면 그쪽을 씁니다. 미래 날짜와 1990년 이전 날짜는 믿지 않습니다(범위는 설정에서).

## 알아보는 파일명

| 출처 | 예 |
| --- | --- |
| 카카오톡 | `KakaoTalk_20240315_123456789.jpg`, `KakaoTalk_Photo_2024-03-15-12-34-56.jpeg` |
| 삼성 | `20240315_123456.jpg`, `20240315_123456(1).jpg` |
| 구글 포토·픽셀 | `PXL_20240315_123456789.jpg`, `IMG_20240315_123456.jpg` |
| 왓츠앱 | `IMG-20240315-WA0001.jpg` |
| 스크린샷 | `Screenshot_20240315_123456.png`, `스크린샷 2024-03-15 123456.png`, `Screen Shot 2024-03-15 at 12.34.56.png` |
| 네이버 밴드·라인 | `band_20240315.jpg`, `LINE_ALBUM_…_20240315…` |
| 그 밖 | 파일명 어디든 `2024-03-15`, `20240315`, `2024.03.15` |

`IMG_1234.jpg`, `DSC_0001.jpg`처럼 날짜가 없는 이름은 EXIF나 짝 파일에서 찾습니다. 다른 패턴은 설정 → 날짜 탭에 정규식으로 추가할 수 있습니다.

## 폴더 규칙

`{yyyy}/{yyyy-mm-dd}`(기본), `{yyyy}/{yyyy-mm}`, `{yyyy-mm-dd}`, `{yyyy}/{source}/{yyyy-mm-dd}`(`2024/카카오톡/2024-03-15/`) 중에서 고르거나 직접 입력합니다. 치환자: `{yyyy}` `{mm}` `{dd}` `{yyyy-mm}` `{yyyy-mm-dd}` `{source}`(카카오톡·스크린샷·메신저·카메라·기타) `{ext}`. 날짜 형식은 언어와 상관없이 `2024-03-15`입니다.

같은 이름(확장자만 다른) 짝 파일 `.mov`(라이브 포토), `.aae`(아이폰 편집 정보), `.xmp`, RAW 원본은 사진을 따라 함께 움직입니다. 한쪽이 실패하면 둘 다 제자리에 둡니다.

## HEIC 변환만 하려면

폴더 규칙 목록 맨 아래의 **정리 안 함 — HEIC 변환만**을 고르고 실행하면, 폴더는 그대로 두고 HEIC 옆에 같은 이름의 JPG만 만듭니다. 윈도우 코덱을 설치할 필요가 없고, 사진을 어디에도 올리지 않습니다.

- 촬영일·방향·GPS·카메라 정보(EXIF)와 색 프로파일(아이폰 Display P3)을 그대로 옮깁니다. 옆으로 눕는 문제를 막으려고 사진을 실제로 돌려 저장합니다.
- 품질 92 기본(설정에서 80~100), 원본 HEIC는 그대로 두거나 `_원본HEIC` 폴더로 옮길 수 있습니다. 삭제는 하지 않습니다.
- 라이브 포토의 `.mov`는 변환하지 않습니다. 연속 촬영 HEIC는 첫 장만 바꿉니다.

명령줄: `python main.py D:\사진 --heic-only`

## 중복 찾기

| 단계 | 방법 | 기본 |
| --- | --- | --- |
| 완전 동일 | 내용이 같은 파일(SHA-1). 하나만 남기고 `_중복`으로 | 켬 |
| 같은 사진 다른 형식 | HEIC와 그 JPG, RAW와 그 JPG | 짝으로 보고 중복으로 치지 않음 |
| 비슷한 사진 | 지각 해시(pHash), 회전도 비교. 그룹만 보여 주고 남길 것은 사람이 고름 | 끔(시작 전에 예상 시간을 보여 줌) |

카카오톡으로 받은 압축본, 해상도가 작은 사본, 다시 저장한 사본은 "중복 추천"으로 표시합니다. 연사처럼 어느 것이 좋은지 사람만 아는 경우는 추천하지 않습니다.

## 명령줄

```bash
python main.py D:\사진 --dry-run
python main.py D:\사진 --pattern "{yyyy}/{yyyy-mm}" --dest E:\정리 --copy --convert-heic --dedupe
python main.py D:\사진 --undo
```

옵션: `--pattern`, `--dest`, `--copy`, `--convert-heic`, `--heic-only`, `--dedupe`, `--similar`, `--use-mtime`, `--include-nodate`, `--dry-run`, `--undo`, `--lang ko|en|zh-CN|ja`

## 하지 않는 것

- 사진을 편집하지 않습니다(자르기·보정 없음). 변환할 때 방향만 바로 세웁니다.
- 얼굴 인식·장소 인식·사람별 분류를 하지 않습니다.
- 클라우드(구글 포토·아이클라우드·원드라이브)에 접속하지 않습니다. 원드라이브·아이클라우드의 "클라우드 전용" 파일은 열지 않고 건너뜁니다(열면 내려받기가 시작되므로).
- EXIF를 고치지 않습니다. 날짜를 표에서 고쳐도 폴더만 바뀝니다.
- GPS 좌표를 화면에 보여 주지 않습니다(변환할 때 그대로 옮기기만).
- 파일을 바로 삭제하지 않습니다. 중복은 `_중복` 폴더로(설정에서 휴지통 선택 가능).

## 시리즈

- 책갈피 툴: [음악 폴더 정리](https://github.com/microhan1/music-folder-organizer) · [음악 정보 채우기](https://github.com/microhan1/music-tag-filler)
- [책갈피 라이브러리](https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=github&utm_medium=referral&utm_campaign=tool_cta&utm_content=photoorganizer) — 읽은 책과 독서록을 기록하는 웹 서비스. 사진을 정리하다 그해 읽은 책이 떠오르면 한 줄 남겨 보세요.

## 라이선스

MIT. 사용한 라이브러리: Pillow, pillow-heif(libheif), piexif, sv-ttk, tkinterdnd2.
