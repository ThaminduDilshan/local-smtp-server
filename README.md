# Local SMTP Server

A tiny local SMTP server that captures emails and provides a web inbox UI.

YAML config works out of the box using the built-in parser.
If you want full YAML feature support, optionally install PyYAML:

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

## Thunder config example

Use this in your Thunder config file:

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

Notes:
- `enable_start_tls` must be `false` for this local server.
- Authentication is accepted for any credentials in local dev mode.

## Optional flags

```bash
python3 server.py --config config.yaml --smtp-host 0.0.0.0 --smtp-port 2525 --http-host 0.0.0.0 --http-port 8025 --max-messages 1000 --refresh-ms 2000
```
