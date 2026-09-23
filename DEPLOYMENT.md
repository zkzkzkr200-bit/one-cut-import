# 한 컷 가져오기 서버

Render 무료 Python 웹 서비스. 실행: `python app.py`.
빌드: `pip install -r requirements.txt && python build_runtime.py`.
`IMPORT_TOKEN`은 24자 이상 무작위 비밀번호로 Render 환경 변수에만 설정하세요. 저장소에 기록하지 마세요.
`ALLOWED_ORIGIN`은 한 컷의 정확한 HTTPS 주소입니다.

최대 30분 원본에서 0.2초~5분 구간을 720p 이하로 가져옵니다. 한 번에 한 작업만 처리합니다.
클라이언트 전달 뒤 삭제하거나 작업 생성 15분 후 임시 파일을 정리합니다. 원본 보관 서비스가 아닙니다.
유튜브 접근 제한이나 변경에 따라 실패할 수 있습니다. 계정 쿠키/프록시로 접근 제한을 우회하지 않습니다.
공개 코드에는 사용자 영상과 인증 정보를 포함하지 않습니다.

검사: `python -m unittest -v test_app` (유튜브 외부 접근은 별도 시험 필요).
