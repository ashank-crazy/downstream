# stack/

A local Switch server for running Downstream: Switch's own standalone compose
file (`docker-compose.yml`, copied from
[sandbox-quantum/switch](https://github.com/sandbox-quantum/switch/blob/main/deploy/local/standalone-docker-compose.yml)),
its `.env.example` with `SWITCH_VERSION` pinned to `0.31.0` and Postgres
published on 5434, and its secret generator.

```bash
cp .env.example .env && bash generate-env-secrets.sh .env downstream
docker compose --env-file .env -p downstream --profile collab --profile gateway up -d
docker compose --env-file .env -p downstream down        # stop (add -v to wipe data)
```
