# Discord 음성 클립 봇

음성 채널에서 **동의한 사용자의 음성만** 최근 60초간 RAM 에 보관하고, `/clip` 실행 시 해당 구간을
원래 대화 타이밍대로 믹싱해 WAV(필요 시 MP3)로 업로드하는 봇입니다.

## ⚠️ 먼저 읽을 것: 음성 수신의 기술적 상태 (2026-11 조사 기준)

| 항목 | 상태 |
|---|---|
| discord.py 본체의 음성 **수신** | **공식 지원 없음.** 송신(재생)만 공식 기능입니다. |
| Discord DAVE(E2EE) 의무화 | 2026-03-02 부터 DAVE 미지원 클라이언트는 통화 참여 불가. discord.py 2.7+ 는 `davey` 로 DAVE 핸드셰이크를 지원하지만, 이는 연결/송신 측입니다. |
| `discord-ext-voice-recv` (비공식 수신 확장) | 업스트림에는 **DAVE 복호화가 병합되지 않았습니다.** DAVE 복호화를 추가하는 PR #54 는 조사 시점에 미병합이고, 일부 사용자가 패치 포크로 성공했다고 보고했지만 `corrupted stream`, `DecryptionFailed` 등 불안정 사례도 함께 보고되었습니다. |
| 이 프로젝트에서 **내가 검증한 것** | 오디오 파이프라인(버퍼·믹서·내보내기·동의 게이트·타임스탬프 정렬) 단위 테스트 26개 통과. |
| 이 프로젝트에서 **검증하지 못한 것** | 실제 Discord 서버에서의 수신. 개발 샌드박스에 네트워크/패키지 설치가 막혀 있고 실서버 접속이 금지되어 `discord.py`·voice-recv 를 import 조차 해보지 못했습니다. `src/bot.py`, `src/commands/*`, `VoiceRecvReceiver` 는 문법 검사만 했고 **첫 실행에서 수정이 필요할 수 있습니다.** |

설계 원칙: 수신이 되는 척하지 않습니다.
- `/join` 은 voice-recv 가 없으면 **참가를 거부**하고 사유를 알립니다.
- voice-recv 소스에 DAVE 관련 코드가 없어 보이면 경고를 띄웁니다 (소스 검사 휴리스틱이며 확정 증거는 아님).
- 실제 수신 여부는 `/status` 의 **수신 통계**(수집 프레임 수, 마지막 수신 시각)로 판단하세요. 동의한 사람이 말하는데 "수집 0" 이면 수신이 실패하는 것입니다.
- 수신부는 `src/audio/receiver.py` 의 `AudioReceiver` 인터페이스 뒤에 분리돼 있어, 다른 라이브러리(예: 공식 지원이 생긴 discord.py, 다른 포크)로 클래스 하나만 갈아끼우면 됩니다. 동의 게이트·버퍼·믹서는 그대로 재사용됩니다.

### DAVE 복호화가 되는 voice-recv 설치 (권장)
`requirements.txt` 의 `discord-ext-voice-recv`(PyPI 판)는 DAVE 복호화가 없을 수 있습니다. DAVE 패치가 들어간 빌드를 쓰려면
[imayhaveborkedit/discord-ext-voice-recv PR #54](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54) 의 코드를 가진 포크/커밋을 설치하세요. 예:

```powershell
pip uninstall -y discord-ext-voice-recv
pip install "git+https://github.com/<포크 소유자>/discord-ext-voice-recv.git@<검토한 커밋>"
```
설치 전 해당 커밋의 코드를 직접 확인하고, 병합 상태(upstream 릴리스 포함 여부)를 다시 확인하세요. 이슈 스레드에서 `rdphillips7` 포크가 언급되지만 저는 그 커밋을 검증하지 않았습니다.

## 기능

| 명령어 | 설명 |
|---|---|
| `/join` | 내가 있는 음성 채널에 참가 + 녹음 안내 (미동의자가 있을 때만 동의 버튼) |
| `/leave` | 퇴장, **RAM 버퍼·임시 데이터 삭제** |
| `/clip [duration] [user] [user2] [user3] [format]` | 직전 N초(기본 30, 최대 60). 사용자를 최대 3명까지 골라 그 사람들만 추출. 사람마다 음량을 맞추고 깨짐 방지 처리 |
| `/soundboard file [name]` | 오디오 파일을 서버장에게 DM 으로 건의. 수락하면 서버장이 이름/이모지를 적고 자동 등록 |
| `/status` | 참가 상태, 참여자별 동의(✅/⛔), 버퍼 사용량, 수신 통계 |
| `/consent` | 음성 수집 동의 |

클립 메시지 아래 버튼: **자르기**(시작~끝 초 입력), **사운드보드**(5.2초 이하일 때 서버장에게 건의).
사운드보드로 낸 소리는 `on_voice_channel_effect` 이벤트로 감지해 CDN 에서 같은 파일을 받아 클립에 합칩니다 (동의한 사용자만).


## 개인정보 설계
- 동의하지 않은 사용자의 PCM 은 버퍼에 닿기 전에 폐기됩니다 (`FrameCollector`). 내용을 저장·처리하지 않습니다.
- 동의 기록은 `data/bot.sqlite3` 에 저장됩니다. 동의를 지우려면 `consents` 테이블의 해당 행을 삭제하세요 (`/withdraw` 는 제거됨).
- 음성은 RAM 에만 있습니다. 디스크에는 `/clip` 결과 파일(`clips/`)과 SQLite 의 동의 기록(길드ID·유저ID·시각)만 저장됩니다. DB 에 오디오 컬럼은 없습니다 (테스트로 확인).
- 새 참가자가 들어오면 채널에 안내와 동의 버튼을 게시합니다. 동의 전에는 수집되지 않습니다.
- 클립 파일은 자동 삭제되지 않습니다. `clips/` 를 주기적으로 정리하세요.
- 법적 고지: 지역에 따라 녹음에 대한 법적 요건이 다릅니다. 서버 규칙에 녹음 사실을 명시하세요.

## Windows 설치 및 실행

1. Python 3.11+ 설치 (설치 화면에서 *Add python.exe to PATH* 체크)
2. FFmpeg 설치: `winget install Gyan.FFmpeg` 후 새 터미널에서 `ffmpeg -version` 확인 (MP3 변환에 필요)
3. 프로젝트 폴더에서:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   copy .env.example .env
   notepad .env        # DISCORD_TOKEN, DEV_GUILD_ID 입력
   python -m pytest    # 테스트
   python -m src.main  # 실행
   ```
   `discord-ext-voice-recv` 설치/PyNaCl 빌드 문제가 있으면 위 DAVE 안내를 먼저 보세요.
4. 종료는 `Ctrl+C` (세션 정리 후 종료).

## Discord Developer Portal 설정
1. https://discord.com/developers/applications → **New Application**
2. **Bot** 탭 → **Reset Token** 으로 토큰 발급 → `.env` 의 `DISCORD_TOKEN` 에 입력 (코드/커밋/채팅에 붙여넣지 마세요)
3. **Privileged Gateway Intents**: 모두 꺼도 됩니다. (참가자 이름이 숫자 ID 로 보이면 *Server Members Intent* 를 켜고 `bot.py` 의 intents 에 `intents.members = True` 추가)
4. **Installation / OAuth2 → URL Generator**: Scopes `bot`, `applications.commands`
5. Bot Permissions: `View Channels`, `Send Messages`, `Attach Files`, `Connect` (송신하지 않으므로 `Speak` 불필요)
6. 생성된 URL 로 **본인의 테스트 서버**에 초대. 테스트 서버 ID 를 `DEV_GUILD_ID` 에 넣으면 명령어가 즉시 반영됩니다.

## 프로젝트 구조
```
src/
  main.py  bot.py  config.py
  commands/ voice.py clip.py consent.py
  audio/    receiver.py(수신 인터페이스+동의 게이트+타임스탬프 정렬)  buffer.py  decoder.py  mixer.py  exporter.py
  database/ db.py
tests/      test_audio.py  test_consent.py
```

## 제한 사항 / 알려진 한계
- 실서버 수신은 위 표대로 미검증입니다. DAVE 환경에서 voice-recv 패치 빌드가 없으면 수신 자체가 실패합니다.
- 스테레오 48kHz WAV 는 초당 약 192KB 라 60초 ≈ 11.5MB. 업로드 한도(기본 8MB 설정) 초과 시 MP3 로 대체되며, FFmpeg 가 없으면 오류로 안내합니다.
- 재연결: discord.py 의 `reconnect=True` 에 맡기고, 완전히 끊기면 세션을 정리하고 `/join` 재실행을 안내합니다. 자동 재참가는 하지 않습니다.
- 말이 없는 구간은 Discord 가 패킷을 보내지 않으므로 타임스탬프 배치로 무음이 채워집니다. 정렬은 RTP 타임스탬프 기반이며 사용자 간 절대 동기는 도착 시각 정밀도(수 ms~수십 ms)에 의존합니다.
- `/clip` 은 봇과 같은 음성 채널에 있는 사용자만 실행할 수 있고, 길드당 동시 클립은 2개까지입니다.
- 스테이지 채널은 지원 대상이 아닙니다.

## 보안 참고
첨부하신 `Fax` 봇 코드(`35f11333-attachment.txt`)에 **실제 Discord 토큰이 평문으로 들어 있습니다.** 이미 공유된 것으로 보고 Developer Portal 에서 즉시 **Reset Token** 하세요. 이 프로젝트는 그 토큰을 사용하지 않았습니다.
