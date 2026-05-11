# Local SMTP Server

A tiny local SMTP server that captures emails with a web inbox UI.

<img width="800" height="400" alt="Screenshot 2026-05-12 at 12 32 33 AM" src="https://github.com/user-attachments/assets/fe2f6093-a54b-4177-9c58-ba291e7f1e61" />

## Prerequisites

- Python3

> YAML config works out of the box using the built-in parser. If you want full YAML feature support, optionally install PyYAML:

  ```bash
  pip install pyyaml
  ```

## Run

```bash
python3 server.py
```

The server will read `config.yaml` by default.

Default ports:
- SMTP: `127.0.0.1:2525`
- HTTP UI: `127.0.0.1:8025`

Open http://127.0.0.1:8025 to view incoming emails.

## Config file (YAML)

Edit `config.yaml`:

```yaml
smtp_host: 127.0.0.1
smtp_port: 2525
smtp_username: dev
smtp_password: dev
smtp_auth_methods: PLAIN, LOGIN, CRAM-MD5
smtp_tls: false
http_host: 127.0.0.1
http_port: 8025
max_messages: 500
refresh_ms: 3000
```

CLI flags still work and override values from the config file.

## ThunderID config example

Use this in your [ThunderID](https://github.com/thunder-id/thunder-id) deployment.yaml file:

```yaml
email:
  smtp:
    host: "127.0.0.1"
    port: 2525
    username: "dev"
    password: "dev"
    from_address: "noreply@thunder.sky"
    enable_start_tls: false
    enable_authentication: true
```

## Optional flags

```bash
python3 server.py --config config.yaml --smtp-host 0.0.0.0 --smtp-port 2525 --http-host 0.0.0.0 --http-port 8025 --max-messages 1000 --refresh-ms 2000
```
