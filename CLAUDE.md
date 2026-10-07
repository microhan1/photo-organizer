# photo-organizer 작업 규칙

PRD와 결정 사항: [docs/PRD.md](docs/PRD.md). 실제로 있었던 문제와 원인: [docs/LESSONS.md](docs/LESSONS.md).
**문제가 생기면 고친 뒤 반드시 docs/LESSONS.md에 증상·원인·해결·재발 방지를 추가한다.** 작업을 시작하기 전에 형제 저장소 `D:\my\music-folder-organizer\docs\LESSONS.md`도 읽는다.

## 사용자 파일 안전

- 사용자의 실제 사진 폴더에는 절대 실행하지 않는다. 스크래치패드에 복사해서 쓰고, 실행 전후 SHA-1 스냅샷으로 비교한다. 실제 폴더는 읽기(스캔·`--dry-run`)도 사본으로 한다.
- 사용자가 직접 한 실행은 대신 되돌리지 않고 알린다.
- 실제 데이터에는 읽기 전용·숨김 파일, EXIF 없는 파일, 카카오톡 압축본, NFD 이름(맥에서 복사), 클라우드 전용 파일이 있다고 가정한다.

## 코드 규칙

- 되돌리기 수단(로그)이 저장되기 전에는 어떤 파일도 옮기거나 만들지 않는다. 실행 중 journal 쓰기 실패는 `LogWriteError`로 멈춘다. `except OSError: pass`로 저장 실패를 삼키지 않는다. `mover.py`의 로그·파일 함수와 `undo.py`는 music-folder-organizer와 공유한다: 고치면 그쪽에도 반영하고 그쪽 시험을 돌린다.
- 짝 파일(HEIC+JPG, RAW+JPG, 사진+MOV, .aae/.xmp)은 한 Item으로 함께 움직인다. 한쪽이 실패하면 옮긴 쪽을 되돌려 놓는다.
- 재실행은 조용해야 한다(멱등): 제자리에 있는 파일이 먼저 자리를 차지하고, `이름 (2).jpg`는 그대로, 변환 JPG는 HEIC와 같은 이름. 충돌·변환을 바꾸면 재실행 시험을 함께 돌린다.
- 다음 실행의 입력은 이번 실행이 바꾼다: 이름 바꾸기가 붙인 접두어(`patterns.strip_renamed`), 사라지는 짝 파일, 옮겨진 원본, 언어 이름의 폴더. 결정(날짜·출처·폴더)은 이번 실행에서 없어질 파일이나 바뀔 이름을 근거로 삼지 않는다(LESSONS A10·A12·A15·A16). 옵션을 더하거나 규칙을 바꾸면 `tests/test_scenarios.py`를 시드 200개로 돌린다(`PHOTO_FUZZ_SEEDS=200`, 실패한 시드는 `-k seed17`로 재현).
- 모순되는 옵션 조합은 한쪽이 이기게 정하고 화면에 알린다(A14).
- 폴더 걷기에서 심볼릭 링크·junction은 들어가지 않는다(A11).
- 기존 경로의 식별은 `scan.key_of`(대소문자 무시), 새 이름 충돌은 `scan.target_key`(NFC 포함).
- 파일을 여는·나열하는·지우는 모든 곳은 `longpath.fs()`를 거친다. 기록·화면의 경로는 일반 형태(LESSONS A2).
- 클라우드 전용 파일은 `DirEntry.stat()` 속성으로만 판별하고 절대 열지 않는다(열면 내려받기가 시작된다).
- 날짜 판별(`dates.resolve`)은 파일을 다시 열지 않는 순수 함수로 둔다. 옵션을 바꿀 때 재스캔하지 않기 위해서다.
- GUI 작업 스레드 안에서는 위젯과 tk 변수를 건드리지 않는다(필요한 값은 `App.ui()`로 시작 전에 읽는다). "실행"은 `plan_ready()`(화면의 계획이 지금 설정으로 만든 것)일 때만.
- 한 줄에 늘어나는 것과 고정 크기 것이 같이 있으면 고정 크기를 먼저 pack. 나중에 보였다 숨겼다 하는 위젯은 `pack(before=...)`. 줄에 위젯을 더하면 그 줄의 pack 순서를 다시 읽는다.
- 화면 문자열은 전부 `lang/*.json`(4개 언어, 같은 키·같은 치환자). 폴더 이름의 날짜는 언어와 무관하게 ISO.
- 필드·변수 이름과 같은 이름의 모듈은 별칭으로 가져온다(LESSONS A1).

## 검증 순서

1. `python -m pytest tests -q` (3.14와 3.12 둘 다). GUI 시험은 3번 연속 통과해야 하고 `timeout`을 걸고 돌린다.
2. GUI를 바꿨으면 `python tests/shots.py <폴더> <출력> ko en zh-CN ja --min`으로 찍고 **이미지를 직접 연다**. 기본 크기와 최소 크기(960×640), 4개 언어, 짧은 경로와 긴 경로. 검게 나오면 먼저 화면 잠금 확인(LESSONS D1).
3. 실제 폴더 사본으로 실행 → 되돌리기 → 스냅샷 동일 확인.
4. 성능은 최악 입력으로 잰다(같은 이름 수천 개, 10만 파일).

## 환경 함정 (Windows)

- Python 문자열에는 `C:\...` 경로. Git Bash의 `/c/...`는 조용히 실패한다.
- 출력에 한·중·일 문자가 있으면 `PYTHONIOENCODING=utf-8`.
- 백슬래시가 하나라도 든 수정은 heredoc 대신 Edit 도구로(LESSONS D2). Write 도구는 `\uXXXX`를 글자로 바꾼다.
- 빌드: PowerShell에서 `build.bat`을 절대 경로로. 먼저 `Get-Process photo-organizer`로 사용자가 앱을 켜 뒀는지 확인하고, 켜져 있으면 끄지 말고 다른 `--distpath`로. `dist\settings.json`은 지우지 않는다. 묶인 파일은 `python -m PyInstaller.utils.cliutils.archive_viewer --list dist\photo-organizer.exe`로 확인.
- 저장소에서 `python main.py`를 돌린 뒤 생긴 `settings.json`은 삭제한다.
- 테스트에서 Tk는 모듈당 하나만 만든다.
- 커밋 전에 스테이징된 파일에서 개인 경로(`C:\Users\...`)를 검색한다. 로컬 전용 정보는 `CLAUDE.local.md`(.gitignore).
