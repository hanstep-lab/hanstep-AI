"""RabbitMQ Worker(``python -m app.worker``). FastAPI 서버와 별도 프로세스로 돈다.

BE가 커밋 후 발행한 작업 메시지를 받아 처리하고 Spring 내부 API로 상태를 보고한다.
지금은 POLICY_INGEST(정책 문서 적재)만 받는다.
"""
