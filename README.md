# cita_homologacion

## Configuración de WhatsApp

Antes de ejecutar `cita_mejorada_trabajo.py`, configura estas variables de entorno
con las credenciales y los números de tu cuenta de Twilio:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM` (número remitente de WhatsApp, por ejemplo `whatsapp:+...`)
- `TWILIO_WHATSAPP_TO` (número destinatario, por ejemplo `whatsapp:+...`)

En PowerShell, puedes definirlas para la sesión actual así:

```powershell
$env:TWILIO_ACCOUNT_SID = "..."
$env:TWILIO_AUTH_TOKEN = "..."
$env:TWILIO_WHATSAPP_FROM = "whatsapp:+..."
$env:TWILIO_WHATSAPP_TO = "whatsapp:+..."
```

No publiques ni guardes credenciales reales en el repositorio. El programa informa
explícitamente si falta alguna variable requerida.
