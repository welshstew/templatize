### Bug: login fails on Safari 18.1

**Version:** 3.4.2
**Browser:** Safari 18.1 on macOS 15.1
**Reported by:** @mkowalski

Steps:
1. Open https://app.acme.example.com/login
2. Sign in as test-user@acme.example.com
3. See "Error 500: upstream timed out" after 30 seconds

Logs from api-gateway-6b7f9c-2xq8z:

```
2026-12-04T10:12:44.118Z ERROR request_id=req_8Kq2Zt upstream=auth-svc:7000 timeout=30s
```
