# CREFLE Reports 서버

`proposals/` 에 보관된 HTML 보고서를, 자동 생성되는 목차(TOC)와 함께 열람할 수 있는 자체 HTML 서버입니다.
기존 GitHub Pages 게시 방식을 대체합니다.

- **언어/프레임워크**: Python · FastAPI + uvicorn
- **목차 자동 생성**: 서버가 `proposals/` 폴더를 스캔해 매 요청마다 최신 목록을 만듭니다. 문서를 추가해도 색인을 손볼 필요가 없습니다.
- **접근 제한**: 모든 경로가 HTTP Basic Auth(아이디/비밀번호)로 보호됩니다.

## 설치

```bash
cd /Users/rangkim/projects/crefle/reports
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 실행

```bash
# (권장) 운영용 자격증명을 환경변수로 설정
export REPORTS_USER="crefle"
export REPORTS_PASS="원하는_강한_비밀번호"

python3 server.py
```

- 기동되면 `0.0.0.0:8000` 으로 바인딩됩니다.
- 같은 네트워크의 동료는 브라우저에서 `http://<서버-IP>:8000` 으로 접속합니다(접속 시 아이디/비밀번호 입력).
- 내 IP 확인: `ipconfig getifaddr en0` (macOS).

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `REPORTS_USER` | `crefle` | Basic Auth 사용자명 |
| `REPORTS_PASS` | `crefle` | Basic Auth 비밀번호 — **운영 시 반드시 변경** |
| `HOST` | `0.0.0.0` | 바인딩 주소 (개인 PC 전용이면 `127.0.0.1`) |
| `PORT` | `8000` | 포트 |
| `REPORTS_DOCS_DIR` | `proposals` | 문서 루트(서버 위치 기준 상대 경로) |

> `REPORTS_PASS` 를 설정하지 않으면 기본값으로 기동하며 시작 로그에 경고가 출력됩니다.

## 동작 방식

- `GET /` → `proposals/` 를 스캔해 목차 페이지를 동적 생성. 폴더별로 묶고 그룹 내 최신순 정렬.
- `GET /<경로>` → 문서·에셋 파일 제공. **`proposals/` 범위 밖**(예: `server.py`, `.git`)이나 경로 트래버설은 404.
  문서의 상대 에셋(`colors_and_type.css`, `assets/…svg`, 폰트 등)이 디렉터리 구조 그대로 로드됩니다.

## 문서 추가

`proposals/` 하위에 `.html` 파일을 두면 자동으로 목차에 나타납니다(서버 재시작 불필요).
새 폴더의 섹션 이름을 예쁘게 표시하려면 `server.py` 의 `GROUP_LABELS` 에 매핑을 추가하세요.

## 지속 실행 (선택)

- 빠른 백그라운드 실행: `nohup python3 server.py > server.log 2>&1 &`
- macOS 상시 운영: `launchd` (`~/Library/LaunchAgents/*.plist`)
- Linux 서버 운영: `systemd` 서비스 유닛
- 인터넷 공개가 필요하면 앞단에 `nginx`/`Caddy` 리버스 프록시로 HTTPS·도메인 연결(이번 범위 밖).
